from collections import OrderedDict

import torch
import torchaudio
from torch import nn
import torch.nn.functional as F
from torchvision.models import efficientnet_b0
from torch.nn import TransformerEncoder, TransformerEncoderLayer

class AudioBackbone(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.config = config


class AttentionPool2d(nn.Module):
    """https://github.com/openai/CLIP/blob/main/clip/model.py"""

    def __init__(
        self, spacial_dim: int, embed_dim: int, num_heads: int, output_dim: int = None
    ):
        super().__init__()
        self.positional_embedding = nn.Parameter(
            torch.randn(spacial_dim, embed_dim) / embed_dim**0.5
        )
        self.k_proj = nn.Linear(embed_dim, embed_dim)
        self.q_proj = nn.Linear(embed_dim, embed_dim)
        self.v_proj = nn.Linear(embed_dim, embed_dim)
        self.c_proj = nn.Linear(embed_dim, output_dim or embed_dim)
        self.num_heads = num_heads

    def forward(self, x):
        x = x.reshape(x.shape[0], x.shape[1], x.shape[2] * x.shape[3]).permute(
            2, 0, 1
        )  # NCHW -> (HW)NC
        x = torch.cat([x.mean(dim=0, keepdim=True), x], dim=0)  # (HW+1)NC
        x = x + self.positional_embedding[:, None, :].to(x.dtype)  # (HW+1)NC
        x, _ = F.multi_head_attention_forward(
            query=x,
            key=x,
            value=x,
            embed_dim_to_check=x.shape[-1],
            num_heads=self.num_heads,
            q_proj_weight=self.q_proj.weight,
            k_proj_weight=self.k_proj.weight,
            v_proj_weight=self.v_proj.weight,
            in_proj_weight=None,
            in_proj_bias=torch.cat(
                [self.q_proj.bias, self.k_proj.bias, self.v_proj.bias]
            ),
            bias_k=None,
            bias_v=None,
            add_zero_attn=False,
            dropout_p=0,
            out_proj_weight=self.c_proj.weight,
            out_proj_bias=self.c_proj.bias,
            use_separate_proj_weight=True,
            training=self.training,
            need_weights=False,
        )

        return x[0]


class Bottleneck(nn.Module):
    """https://github.com/openai/CLIP/blob/main/clip/model.py"""

    expansion = 4

    def __init__(self, inplanes, planes, stride=1):
        super().__init__()

        # all conv layers have stride 1. an avgpool is performed after the second convolution when stride > 1
        self.conv1 = nn.Conv2d(inplanes, planes, 1, bias=False)
        self.bn1 = nn.BatchNorm2d(planes)

        self.conv2 = nn.Conv2d(planes, planes, 3, padding=1, bias=False)
        self.bn2 = nn.BatchNorm2d(planes)

        self.avgpool = nn.AvgPool2d(stride) if stride > 1 else nn.Identity()

        self.conv3 = nn.Conv2d(planes, planes * self.expansion, 1, bias=False)
        self.bn3 = nn.BatchNorm2d(planes * self.expansion)

        self.relu = nn.ReLU(inplace=True)
        self.downsample = None
        self.stride = stride

        if stride > 1 or inplanes != planes * Bottleneck.expansion:
            # downsampling layer is prepended with an avgpool, and the subsequent convolution has stride 1
            self.downsample = nn.Sequential(
                OrderedDict(
                    [
                        ("-1", nn.AvgPool2d(stride)),
                        (
                            "0",
                            nn.Conv2d(
                                inplanes,
                                planes * self.expansion,
                                1,
                                stride=1,
                                bias=False,
                            ),
                        ),
                        ("1", nn.BatchNorm2d(planes * self.expansion)),
                    ]
                )
            )

    def forward(self, x: torch.Tensor):
        identity = x

        out = self.relu(self.bn1(self.conv1(x)))
        out = self.relu(self.bn2(self.conv2(out)))
        out = self.avgpool(out)
        out = self.bn3(self.conv3(out))

        if self.downsample is not None:
            identity = self.downsample(x)

        out += identity
        out = self.relu(out)
        return out


class ModifiedResNet(nn.Module):
    """https://github.com/openai/CLIP/blob/main/clip/model.py"""

    """
    A ResNet class that is similar to torchvision's but contains the following changes:
    - There are now 3 "stem" convolutions as opposed to 1, with an average pool instead of a max pool.
    - Performs anti-aliasing strided convolutions, where an avgpool is prepended to convolutions with stride > 1
    - The final pooling layer is a QKV attention instead of an average pool
    """

    def __init__(self, config):
        super().__init__()
        sample_rate = config.sample_rate
        n_fft = config.n_fft
        f_min = config.f_min
        f_max = config.f_max
        n_mels = config.n_mels

        self.pooling = config.pooling

        self.spec = torchaudio.transforms.MelSpectrogram(
            sample_rate=sample_rate,
            n_fft=n_fft,
            f_min=f_min,
            f_max=f_max,
            n_mels=n_mels,
            power=2,
            normalized=True,
        )
        self.amplitude_to_db = torchaudio.transforms.AmplitudeToDB(top_db=100)

        conv_out_channels = config.conv_out_channels

        layers = (3, 4, 6, 3)
        output_dim = config.hidden_size
        heads = 8
        width = conv_out_channels

        self.output_dim = output_dim

        # the 3-layer stem
        self.conv1 = nn.Conv2d(
            1, width // 2, kernel_size=3, stride=2, padding=1, bias=False
        )
        self.bn1 = nn.BatchNorm2d(width // 2)

        self.conv2 = nn.Conv2d(
            width // 2, width // 2, kernel_size=3, padding=1, bias=False
        )
        self.bn2 = nn.BatchNorm2d(width // 2)

        self.conv3 = nn.Conv2d(width // 2, width, kernel_size=3, padding=1, bias=False)
        self.bn3 = nn.BatchNorm2d(width)

        self.avgpool = nn.AvgPool2d(2)
        self.relu = nn.ReLU(inplace=True)

        # residual layers
        self._inplanes = width  # this is a *mutable* variable used during construction
        self.layer1 = self._make_layer(width, layers[0])
        self.layer2 = self._make_layer(width * 2, layers[1], stride=2)
        self.layer3 = self._make_layer(width * 4, layers[2], stride=2)
        self.layer4 = self._make_layer(width * 8, layers[3], stride=2)

        if self.pooling == "attention":
            embed_dim = width * 32  # the ResNet feature dimension
            f_out_dim = n_mels // 32
            t_out_dim = (
                (config.audio_len_seconds * sample_rate) // (n_fft // 2) + 1
            ) // 32
            fc_dim = f_out_dim * t_out_dim + 1
            self.attnpool = AttentionPool2d(fc_dim, embed_dim, heads, output_dim)
        elif self.pooling == "average":
            self.final_avgpool = torch.nn.AdaptiveAvgPool2d((1, 1))
            self.final_linear = nn.Linear(512, 256)

    def _make_layer(self, planes, blocks, stride=1):
        layers = [Bottleneck(self._inplanes, planes, stride)]

        self._inplanes = planes * Bottleneck.expansion
        for _ in range(1, blocks):
            layers.append(Bottleneck(self._inplanes, planes))

        return nn.Sequential(*layers)

    def forward(self, x):
        spec = self.spec(x)
        spec = self.amplitude_to_db(spec)
        x = spec.unsqueeze(1)

        def stem(x):
            for conv, bn in [
                (self.conv1, self.bn1),
                (self.conv2, self.bn2),
                (self.conv3, self.bn3),
            ]:
                x = self.relu(bn(conv(x)))
            x = self.avgpool(x)
            return x

        x = x.type(self.conv1.weight.dtype)
        x = stem(x)
        x = self.layer1(x)
        x = self.layer2(x)
        x = self.layer3(x)
        x = self.layer4(x)

        if self.pooling == "attention":
            x = self.attnpool(x)
        elif self.pooling == "average":
            x = self.final_avgpool(x).squeeze()
            x = self.final_linear(x)

        return x


class AudioCNN(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.setup_mel_spectrogram(config)

        self.conv_layers = nn.Sequential(
            self.conv_block(1, 64, kernel_size=3, stride=1, padding=1),
            self.conv_block(64, 128, kernel_size=3, stride=2, padding=1),
            self.conv_block(128, 256, kernel_size=3, stride=2, padding=1),
            self.conv_block(256, 512, kernel_size=3, stride=2, padding=1),
        )

        self.adaptive_pool = nn.AdaptiveAvgPool2d((1, 1))
        self.fc = nn.Linear(512, config.hidden_size)

    def conv_block(self, in_channels, out_channels, **kwargs):
        return nn.Sequential(
            nn.Conv2d(in_channels, out_channels, **kwargs),
            nn.BatchNorm2d(out_channels),
            nn.ReLU(inplace=True)
        )

    def setup_mel_spectrogram(self, config):
        self.spec = torchaudio.transforms.MelSpectrogram(
            sample_rate=config.sample_rate,
            n_fft=config.n_fft,
            f_min=config.f_min,
            f_max=config.f_max,
            n_mels=config.n_mels,
            power=2,
            normalized=True,
        )
        self.amplitude_to_db = torchaudio.transforms.AmplitudeToDB(top_db=100)

    def forward(self, x):
        spec = self.spec(x)
        spec = self.amplitude_to_db(spec)
        x = spec.unsqueeze(1)

        x = self.conv_layers(x)
        x = self.adaptive_pool(x)
        x = torch.flatten(x, 1)
        x = self.fc(x)

        return x



class AudioEfficientNet(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.setup_mel_spectrogram(config)

        self.efficientnet = efficientnet_b0(pretrained=True)
        self.efficientnet.features[0][0] = nn.Conv2d(1, 32, kernel_size=3, stride=2, padding=1, bias=False)
        self.efficientnet.classifier = nn.Sequential(
            nn.Dropout(p=0.2, inplace=True),
            nn.Linear(1280, config.hidden_size),
        )

    def setup_mel_spectrogram(self, config):
        self.spec = torchaudio.transforms.MelSpectrogram(
            sample_rate=config.sample_rate,
            n_fft=config.n_fft,
            f_min=config.f_min,
            f_max=config.f_max,
            n_mels=config.n_mels,
            power=2,
            normalized=True,
        )
        self.amplitude_to_db = torchaudio.transforms.AmplitudeToDB(top_db=100)

    def forward(self, x):
        spec = self.spec(x)
        spec = self.amplitude_to_db(spec)
        x = spec.unsqueeze(1)

        x = self.efficientnet(x)
        return x


class AudioTransformer(nn.Module):
    def __init__(self, config):
        super(AudioTransformer, self).__init__()
        self.config = config
        self.setup_mel_spectrogram(config)

        self.d_model = config.get('hidden_size', 512)
        self.num_layers = config.get('num_layers', 6)
        self.num_heads = config.get('num_heads', 8)
        self.dim_feedforward = config.get('dim_feedforward', 2048)
        self.dropout = config.get('dropout', 0.1)
        self.output_size = config.get('output_size', 256)

        self.positional_encoding = PositionalEncoding(self.d_model, self.dropout)

        encoder_layers = TransformerEncoderLayer(
            d_model=self.d_model,
            nhead=self.num_heads,
            dim_feedforward=self.dim_feedforward,
            dropout=self.dropout
        )
        self.transformer_encoder = TransformerEncoder(encoder_layers, self.num_layers)
        self.fc = nn.Linear(self.d_model, self.output_size)

    def setup_mel_spectrogram(self, config):
        self.spec = torchaudio.transforms.MelSpectrogram(
            sample_rate=config.sample_rate,
            n_fft=config.n_fft,
            f_min=config.f_min,
            f_max=config.f_max,
            n_mels=config.n_mels,
            power=2,
            normalized=True,
        )
        self.amplitude_to_db = torchaudio.transforms.AmplitudeToDB(top_db=100)

    def forward(self, x):
        spec = self.spec(x)
        spec = self.amplitude_to_db(spec)
        x = spec.unsqueeze(1)
        x = x.reshape(x.size(0), -1, self.d_model)

        x = self.positional_encoding(x)
        x = self.transformer_encoder(x)

        x = x.mean(dim=1)
        x = self.fc(x)

        return x

class PositionalEncoding(nn.Module):
    def __init__(self, d_model, dropout=0.1, max_len=5000):
        super().__init__()
        self.dropout = nn.Dropout(p=dropout)

        position = torch.arange(0, max_len).unsqueeze(1)
        div_term = torch.exp(torch.arange(0, d_model, 2) * -(torch.log(torch.tensor(10000.0)) / d_model))
        pe = torch.zeros(max_len, 1, d_model)
        pe[:, 0, 0::2] = torch.sin(position * div_term)
        pe[:, 0, 1::2] = torch.cos(position * div_term)
        self.register_buffer('pe', pe)

    def forward(self, x):
        pe = self.pe[:x.size(1), :].expand(x.size(1), x.size(0), -1).permute(1, 0, 2)

        print(f"Positional Encoding shape (after expand): {pe.shape}")
        print(f"Input shape before adding positional encoding: {x.shape}")

        x = x + pe
        return self.dropout(x)


class AudioAutoEncoder(nn.Module):
    def __init__(self, config):
        super(AudioAutoEncoder, self).__init__()
        self.config = config
        self.setup_mel_spectrogram(config)

        self.latent_dim = config.get('latent_dim', 256)
        self.output_size = config.get('output_size', 256)

        self.encoder_linear = nn.Sequential(
            nn.Linear(128 * 1836, 512),
            nn.ReLU(),
            nn.Linear(512, self.latent_dim)
        )

        self.fc_mu = nn.Linear(self.latent_dim, self.output_size)
        self.fc_logvar = nn.Linear(self.latent_dim, self.output_size)

    def setup_mel_spectrogram(self, config):
        self.spec = torchaudio.transforms.MelSpectrogram(
            sample_rate=config.sample_rate,
            n_fft=config.n_fft,
            f_min=config.f_min,
            f_max=config.f_max,
            n_mels=config.n_mels,
            power=2,
            normalized=True,
        )
        self.amplitude_to_db = torchaudio.transforms.AmplitudeToDB(top_db=100)

    def reparameterize(self, mu, logvar):
        std = torch.exp(0.5 * logvar)
        eps = torch.randn_like(std)
        return mu + eps * std

    def forward(self, x):
        spec = self.spec(x)  # (batch_size, n_mels, time_steps)
        spec = self.amplitude_to_db(spec)
        # torch.Size([2, 128, 1836])

        x = spec.unsqueeze(1)  # (batch_size, 1, n_mels, time_steps)
        # torch.Size([2, 1, 128, 1836])

        x = x.reshape(x.size(0), -1)
        # torch.Size([2, 235008])


        encoded = self.encoder_linear(x)
        # torch.Size([2, 256])

        mu = self.fc_mu(encoded)
        logvar = self.fc_logvar(encoded)

        z = self.reparameterize(mu, logvar)
        # torch.Size([2, 256])

        return z
