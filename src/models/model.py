from math import sqrt

import torch
import torch.nn as nn
from torch.autograd import Variable
from torch.nn import functional as F

from src.models.layers import LinearNorm, ConvNorm

# The resulting mask indicates which positions within each sequence are considered valid (within the given lengths) and which positions are considered invalid (beyond the given lengths).
# if len = [2,3,5] mask --> [[t,t,f,f,f],[t,t,t,f,f],[t,t,t,t,t]]
def get_mask_from_lengths(lengths):
    max_len = torch.max(lengths).item()
    ids = torch.arange(0, max_len).to(lengths.device)
    mask = (ids < lengths.unsqueeze(1)).bool()
    return mask


# this for conv it's output is a flatten array 
# it's gives same x,y dim but change the number of attention filters to attention dimensions 
class LocationLayer(nn.Module):
    def __init__(self, attention_n_filters, attention_kernel_size,
                 attention_dim):
        super(LocationLayer, self).__init__()
        padding = int((attention_kernel_size - 1) / 2)
        self.location_conv = ConvNorm(2, attention_n_filters,
                                      kernel_size=attention_kernel_size,
                                      padding=padding, bias=False, stride=1,
                                      dilation=1)
        self.location_dense = LinearNorm(attention_n_filters, attention_dim,
                                         bias=False, w_init_gain='tanh')

    def forward(self, attention_weights_cat):
        # attention_weights_cat [B,2,T]
        processed_attention = self.location_conv(attention_weights_cat)  # [B,32,T]
        processed_attention = processed_attention.transpose(1, 2)  # [B,T,32]
        processed_attention = self.location_dense(processed_attention)  # [B,T,128]
        return processed_attention


class Attention(nn.Module):
    def __init__(self, attention_rnn_dim, embedding_dim, attention_dim,
                 attention_location_n_filters, attention_location_kernel_size):
        super(Attention, self).__init__()
        # 将 query 即 decoder 的输出变换维度
#          A linear layer that transforms the decoder's output (query) from attention_rnn_dim to attention_dim dimensions
        self.query_layer = LinearNorm(attention_rnn_dim, attention_dim,
                                      bias=False, w_init_gain='tanh')

        # 将 memory 即 encoder的输出变换维度
#          A linear layer that transforms the encoder's output (memory) from attention_rnn_dim to attention_dim dimensions
        self.memory_layer = LinearNorm(embedding_dim, attention_dim, bias=False,
                                       w_init_gain='tanh')
#      A linear layer that maps the attention dimension (attention_dim) to a scalar value (1) for each time step. 
        self.v = LinearNorm(attention_dim, 1, bias=False)
        self.location_layer = LocationLayer(attention_location_n_filters,
                                            attention_location_kernel_size,
                                            attention_dim)
        self.score_mask_value = -float("inf")
# These energies indicate the relevance or importance of each time step in the input sequence for the current decoding step. like nlp example in lec
    def get_alignment_energies(self, query, processed_memory,
                               attention_weights_cat):
        """
        PARAMS
        ------
        query: decoder output (batch, n_mel_channels * n_frames_per_step)
        processed_memory: processed encoder outputs (B, T_in, attention_dim)
        attention_weights_cat: cumulative and prev. att weights (B, 2, max_time)

        RETURNS
        -------
        alignment (batch, max_time)
        """

        processed_query = self.query_layer(query.unsqueeze(1))  # [B,1,128]
        processed_attention_weights = self.location_layer(attention_weights_cat)  # [B,T,128]
        # processed_memory   [B,T,128]
        energies = self.v(torch.tanh(
            processed_query + processed_attention_weights + processed_memory))
        # energies [B,T,1]
        energies = energies.squeeze(-1)
        return energies
    
# computes the attention context vector and attention weights based on the provided inputs. 
    def forward(self, attention_hidden_state, memory, processed_memory,
                attention_weights_cat, mask):
        """
        PARAMS
        ------
        attention_hidden_state: attention rnn last output
        memory: encoder outputs
        processed_memory: processed encoder outputs
        attention_weights_cat: previous and cummulative attention weights
        mask: binary mask for padded data
        """
        alignment = self.get_alignment_energies(
            attention_hidden_state, processed_memory, attention_weights_cat)

        if mask is not None:
            alignment.data.masked_fill_(mask, self.score_mask_value)

        attention_weights = torch.softmax(alignment, dim=1)
        attention_context = torch.bmm(attention_weights.unsqueeze(1), memory)
        attention_context = attention_context.squeeze(1)

        return attention_context, attention_weights

# the Prenet module applies a series of linear transformations with ReLU activation and dropout to the input tensor. 
# It helps extract relevant features from the input before passing it to the main network, providing a non-linear transformation and regularization.
class Prenet(nn.Module):
    def __init__(self, in_dim, sizes):
        super(Prenet, self).__init__()
        in_sizes = [in_dim] + sizes[:-1]
        self.layers = nn.ModuleList(
            [LinearNorm(in_size, out_size, bias=False)
             for (in_size, out_size) in zip(in_sizes, sizes)])

    def forward(self, x):
        for linear in self.layers:
            x = F.dropout(F.relu(linear(x)), p=0.5, training=True)
        return x


# class represents the postnet module, which is a stack of 1-dimensional convolutions used for post-processing the outputs of a speech synthesis model.
# The postnet helps refine the predicted mel-spectrogram by adding fine-grained details and reducing potential artifacts.    
class Postnet(nn.Module):
    """Postnet
        - Five 1-d convolution with 512 channels and kernel size 5
    """

    def __init__(self, config):
        super(Postnet, self).__init__()
        self.convolutions = nn.ModuleList()
        'The first convolutional layer is defined using ConvNorm and nn.BatchNorm1d. 
        'It takes the mel-spectrogram channels as input (config.n_mel_channels) and applies a 1-dimensional 
        'convolution with config.postnet_embedding_dim output channels and a kernel size of config.postnet_kernel_size. 
        'The output of this convolution is then passed through batch normalization.'
        self.convolutions.append(
            nn.Sequential(
                ConvNorm(config.n_mel_channels, config.postnet_embedding_dim,
                         kernel_size=config.postnet_kernel_size, stride=1,
                         padding=int((config.postnet_kernel_size - 1) / 2),
                         dilation=1, w_init_gain='tanh'),
                nn.BatchNorm1d(config.postnet_embedding_dim))
        )

        # For the intermediate convolutions, the same pattern is followed: 1-dimensional convolution, batch normalization, and activation function (tanh)

        for i in range(1, config.postnet_n_convolutions - 1):
            self.convolutions.append(
                nn.Sequential(
                    ConvNorm(config.postnet_embedding_dim,
                             config.postnet_embedding_dim,
                             kernel_size=config.postnet_kernel_size, stride=1,
                             padding=int((config.postnet_kernel_size - 1) / 2),
                             dilation=1, w_init_gain='tanh'),
                    nn.BatchNorm1d(config.postnet_embedding_dim))
            )
"The last convolutional layer applies a 1-dimensional convolution with config.postnet_embedding_dim input channels and config.n_mel_channels output channels.
"The kernel size and padding are determined by config.postnet_kernel_size.
"The activation function used here is linear, and batch normalization is applied."
        self.convolutions.append(
            nn.Sequential(
                ConvNorm(config.postnet_embedding_dim, config.n_mel_channels,
                         kernel_size=config.postnet_kernel_size, stride=1,
                         padding=int((config.postnet_kernel_size - 1) / 2),
                         dilation=1, w_init_gain='linear'),
                nn.BatchNorm1d(config.n_mel_channels))
        )
#  forward is applay the multi conv layers takes an input tensor x and processes it through the convolutional layers.
    def forward(self, x):
        for i in range(len(self.convolutions) - 1):
#             For each convolutional layer except the last one, the input tensor x is passed through the convolution, followed by the tanh activation function and dropout (F.dropout).
            x = F.dropout(torch.tanh(self.convolutions[i](x)), 0.5, self.training)
#     For the last convolutional layer, only the convolution and dropout are applied, without the tanh activation.
        x = F.dropout(self.convolutions[-1](x), 0.5, self.training)

        return x


class Encoder(nn.Module):
    """Encoder module:
        - Three 1-d convolution banks
        - Bidirectional LSTM
    """

    def __init__(self, config):
        super(Encoder, self).__init__()

        convolutions = []
        for _ in range(config.encoder_n_convolutions):
            conv_layer = nn.Sequential(
                ConvNorm(config.encoder_embedding_dim,
                         config.encoder_embedding_dim,
                         kernel_size=config.encoder_kernel_size, stride=1,
                         padding=int((config.encoder_kernel_size - 1) / 2),
                         dilation=1, w_init_gain='relu'),
                nn.BatchNorm1d(config.encoder_embedding_dim))
            convolutions.append(conv_layer)
        self.convolutions = nn.ModuleList(convolutions)

        self.lstm = nn.LSTM(config.encoder_embedding_dim,
                            int(config.encoder_embedding_dim / 2), 1,
                            batch_first=True, bidirectional=True)

    def forward(self, x, input_lengths):
        for conv in self.convolutions:
            x = F.dropout(F.relu(conv(x)), 0.5, self.training)

        x = x.transpose(1, 2)

        input_lengths = input_lengths.cpu().numpy()
        x = nn.utils.rnn.pack_padded_sequence(
            x, input_lengths, batch_first=True)

        self.lstm.flatten_parameters()
        outputs, _ = self.lstm(x)

        outputs, _ = nn.utils.rnn.pad_packed_sequence(
            outputs, batch_first=True)

        return outputs

    def inference(self, x):
        """测试时只输入1条数据，不用pack padding 的步骤"""
        for conv in self.convolutions:
            x = F.dropout(F.relu(conv(x)), 0.5, self.training)

        x = x.transpose(1, 2)  # [B,T,C]

        self.lstm.flatten_parameters()
        outputs, _ = self.lstm(x)

        return outputs


# 解码部分        
class Decoder(nn.Module):
    def __init__(self, config):
        super(Decoder, self).__init__()
        # 目标特征维度
        self.n_mel_channels = config.n_mel_channels

        # 每步解码 n_frames_per_step 帧特征
        self.n_frames_per_step = config.n_frames_per_step

        # 编码输出特征的维度, 也就是 attention-context的维度
        self.encoder_embedding_dim = config.encoder_embedding_dim

        # 注意力计算用 RNN 的维度
        self.attention_rnn_dim = config.attention_rnn_dim

        # 解码 RNN 的维度
        self.decoder_rnn_dim = config.decoder_rnn_dim

        # pre-net 的维度
        self.prenet_dim = config.prenet_dim

        # 测试过程中最多解码多少步
        self.max_decoder_steps = config.max_decoder_steps

        # 测试过程中 gate端 输入多少认为解码结束
        self.gate_threshold = config.gate_threshold

        self.p_attention_dropout = config.p_attention_dropout
        self.p_decoder_dropout = config.p_decoder_dropout

        # 定义Prenet
        self.prenet = Prenet(
            config.n_mel_channels * config.n_frames_per_step,
            [config.prenet_dim, config.prenet_dim])

        #  attention rnn 底层RNN
        self.attention_rnn = nn.LSTMCell(
            config.prenet_dim + config.encoder_embedding_dim,
            config.attention_rnn_dim)

        # attention 层
        self.attention_layer = Attention(
            config.attention_rnn_dim, config.encoder_embedding_dim,
            config.attention_dim, config.attention_location_n_filters,
            config.attention_location_kernel_size)

        # decoder RNN 上层 RNN
        self.decoder_rnn = nn.LSTMCell(
            config.attention_rnn_dim + config.encoder_embedding_dim,
            config.decoder_rnn_dim, 1)
        self.drop_decoder_rnn = nn.Dropout(0.1)
        # 线性映射层 
        self.linear_projection = LinearNorm(
            config.decoder_rnn_dim + config.encoder_embedding_dim,
            config.n_mel_channels * config.n_frames_per_step)

        self.gate_layer = LinearNorm(
            config.decoder_rnn_dim + config.encoder_embedding_dim, 1,
            bias=True, w_init_gain='sigmoid')

    def get_go_frame(self, memory):
        """ 
        构造一个全0的矢量作为 decoder 第一帧的输出
        """
        B = memory.size(0)
        decoder_input = Variable(memory.data.new(
            B, self.n_mel_channels * self.n_frames_per_step).zero_())
        return decoder_input

    def initialize_decoder_states(self, memory, mask):

        B = memory.size(0)
        MAX_TIME = memory.size(1)

        self.attention_hidden = Variable(memory.data.new(
            B, self.attention_rnn_dim).zero_())
        self.attention_cell = Variable(memory.data.new(
            B, self.attention_rnn_dim).zero_())

        self.decoder_hidden = Variable(memory.data.new(
            B, self.decoder_rnn_dim).zero_())
        self.decoder_cell = Variable(memory.data.new(
            B, self.decoder_rnn_dim).zero_())

        self.attention_weights = Variable(memory.data.new(
            B, MAX_TIME).zero_())
        self.attention_weights_cum = Variable(memory.data.new(
            B, MAX_TIME).zero_())
        self.attention_context = Variable(memory.data.new(
            B, self.encoder_embedding_dim).zero_())

        self.memory = memory
        self.processed_memory = self.attention_layer.memory_layer(memory)
        self.mask = mask

    def parse_decoder_inputs(self, decoder_inputs):
        """ Prepares decoder inputs, i.e. mel outputs
        PARAMS
        ------
        decoder_inputs: inputs used for teacher-forced training, i.e. mel-specs

        RETURNS
        -------
        inputs: processed decoder inputs

        """
        # (B, n_mel_channels, T_out) -> (B, T_out, n_mel_channels)
        decoder_inputs = decoder_inputs.transpose(1, 2)
        # (B, T_out, n_mel_channels) -> (B, T_out/3, n_mel_channels*3)
        decoder_inputs = decoder_inputs.reshape(
            decoder_inputs.size(0),
            int(decoder_inputs.size(1) / self.n_frames_per_step), -1)
        # (B, T_out, n_mel_channels) -> (T_out, B, n_mel_channels)
        decoder_inputs = decoder_inputs.transpose(0, 1)
        return decoder_inputs

    def parse_decoder_outputs(self, mel_outputs, gate_outputs, alignments):
        """ Prepares decoder outputs for output
        PARAMS
        ------
        mel_outputs:
        gate_outputs: gate output energies
        alignments:

        RETURNS
        -------
        mel_outputs:
        gate_outpust: gate output energies
        alignments:
        """
        # (T_out, B) -> (B, T_out)
        alignments = torch.stack(alignments).transpose(0, 1)
        # (T_out, B) -> (B, T_out)
        gate_outputs = torch.stack(gate_outputs).transpose(0, 1)
        gate_outputs = gate_outputs.contiguous()

        # (T_out, B, n_mel_channels) -> (B, T_out, n_mel_channels)
        mel_outputs = torch.stack(mel_outputs).transpose(0, 1).contiguous()
        # decouple frames per step
        mel_outputs = mel_outputs.view(
            mel_outputs.size(0), -1, self.n_mel_channels)
        # (B, T_out, n_mel_channels) -> (B, n_mel_channels, T_out)
        mel_outputs = mel_outputs.transpose(1, 2)

        return mel_outputs, gate_outputs, alignments

    def decode(self, decoder_input):
        """ Decoder step using stored states, attention and memory
        PARAMS
        ------
        decoder_input: previous mel output

        RETURNS
        -------
        mel_output:
        gate_output: gate output energies
        attention_weights:
        """
        cell_input = torch.cat((decoder_input, self.attention_context), -1)
        self.attention_hidden, self.attention_cell = self.attention_rnn(
            cell_input, (self.attention_hidden, self.attention_cell))
        self.attention_hidden = F.dropout(
            self.attention_hidden, self.p_attention_dropout, self.training)

        attention_weights_cat = torch.cat(
            (self.attention_weights.unsqueeze(1),
             self.attention_weights_cum.unsqueeze(1)), dim=1)
        self.attention_context, self.attention_weights = self.attention_layer(
            self.attention_hidden, self.memory, self.processed_memory,
            attention_weights_cat, self.mask)

        self.attention_weights_cum += self.attention_weights

        decoder_input = torch.cat(
            (self.attention_hidden, self.attention_context), -1)
        self.decoder_hidden, self.decoder_cell = self.decoder_rnn(
            decoder_input, (self.decoder_hidden, self.decoder_cell))
        self.decoder_hidden = F.dropout(
            self.decoder_hidden, self.p_decoder_dropout, self.training)

        decoder_hidden_attention_context = torch.cat(
            (self.decoder_hidden, self.attention_context), dim=1)
        decoder_output = self.linear_projection(
            decoder_hidden_attention_context)

        gate_prediction = self.gate_layer(decoder_hidden_attention_context)
        return decoder_output, gate_prediction, self.attention_weights

    def forward(self, memory, decoder_inputs, memory_lengths):
        """ Decoder forward pass for training
        PARAMS
        ------
        memory: Encoder outputs
        decoder_inputs: Decoder inputs for teacher forcing. i.e. mel-specs
        memory_lengths: Encoder output lengths for attention masking.

        RETURNS
        -------
        mel_outputs: mel outputs from the decoder
        gate_outputs: gate outputs from the decoder
        alignments: sequence of attention weights from the decoder
        
         mel_outputs, gate_outputs, alignments = self.decoder(
            encoder_outputs, mels, memory_lengths=text_lengths)
        
        """

        decoder_input = self.get_go_frame(memory).unsqueeze(0)
        decoder_inputs = self.parse_decoder_inputs(decoder_inputs)
        decoder_inputs = torch.cat((decoder_input, decoder_inputs), dim=0)
        decoder_inputs = self.prenet(decoder_inputs)

        self.initialize_decoder_states(
            memory, mask=~get_mask_from_lengths(memory_lengths))

        mel_outputs, gate_outputs, alignments = [], [], []
        while len(mel_outputs) < decoder_inputs.size(0) - 1:
            decoder_input = decoder_inputs[len(mel_outputs)]
            mel_output, gate_output, attention_weights = self.decode(
                decoder_input)
            mel_outputs += [mel_output.squeeze(1)]
            gate_outputs += [gate_output.squeeze(1)]
            alignments += [attention_weights]

        mel_outputs, gate_outputs, alignments = self.parse_decoder_outputs(
            mel_outputs, gate_outputs, alignments)

        return mel_outputs, gate_outputs, alignments

    def inference(self, memory):
        """ Decoder inference
        PARAMS
        ------
        memory: Encoder outputs

        RETURNS
        -------
        mel_outputs: mel outputs from the decoder
        gate_outputs: gate outputs from the decoder
        alignments: sequence of attention weights from the decoder
        """
        decoder_input = self.get_go_frame(memory)

        self.initialize_decoder_states(memory, mask=None)

        mel_outputs, gate_outputs, alignments = [], [], []
        while True:
            decoder_input = self.prenet(decoder_input)
            mel_output, gate_output, alignment = self.decode(decoder_input)

            mel_outputs += [mel_output.squeeze(1)]
            gate_outputs += [gate_output]
            alignments += [alignment]

            if torch.sigmoid(gate_output) > self.gate_threshold:
                break
            elif len(mel_outputs) == self.max_decoder_steps:
                print("Warning! Reached max decoder steps")
                break

            decoder_input = mel_output

        mel_outputs, gate_outputs, alignments = self.parse_decoder_outputs(
            mel_outputs, gate_outputs, alignments)

        return mel_outputs, gate_outputs, alignments


class Tacotron2(nn.Module):
    def __init__(self, config):
        super(Tacotron2, self).__init__()

        self.n_frames_per_step = config.n_frames_per_step
        self.n_mel_channels = config.n_mel_channels

        self.embedding = nn.Embedding(config.n_symbols, config.symbols_embedding_dim)
        std = sqrt(2.0 / (config.n_symbols + config.symbols_embedding_dim))
        val = sqrt(3.0) * std  # uniform bounds for std
        self.embedding.weight.data.uniform_(-val, val)

        self.encoder = Encoder(config)
        self.decoder = Decoder(config)
        self.postnet = Postnet(config)

    def parse_output(self, outputs, output_lengths=None):
        # mask = ~get_mask_from_lengths(output_lengths)

        # 当每一step预测多帧时，会对mel进行pad，而output_lengths表示mel真实长度，会产生偏差
        # 因此利用pad后的最大长度，即outputs[0].size(-1)，求得mask
        max_len = outputs[0].size(-1)
        ids = torch.arange(0, max_len).to(output_lengths.device)
        mask = (ids < output_lengths.unsqueeze(1)).bool()
        mask = ~mask
        
        mask = mask.expand(self.n_mel_channels, mask.size(0), mask.size(1))
        mask = mask.permute(1, 0, 2)

        outputs[0].data.masked_fill_(mask, 0.0)
        outputs[1].data.masked_fill_(mask, 0.0)
        outputs[2].data.masked_fill_(mask[:, 0, :], 1e3)

        return outputs

    def forward(self, text_inputs, text_lengths, mels, output_lengths):
        # 进行 text 编码
        embedded_inputs = self.embedding(text_inputs).transpose(1, 2)
        # 得到encoder输出
        encoder_outputs = self.encoder(embedded_inputs, text_lengths)

        # 得到 decoder 输出
        mel_outputs, gate_outputs, alignments = self.decoder(encoder_outputs, mels, memory_lengths=text_lengths)

        gate_outputs = gate_outputs.unsqueeze(2).repeat(1, 1, self.n_frames_per_step)
        gate_outputs = gate_outputs.view(gate_outputs.size(0), -1)

        # 进过postnet 得到预测的 mel 输出
        mel_outputs_postnet = self.postnet(mel_outputs)
        mel_outputs_postnet = mel_outputs + mel_outputs_postnet

        return self.parse_output([mel_outputs, mel_outputs_postnet, gate_outputs, alignments], output_lengths)

    def inference(self, inputs):
        embedded_inputs = self.embedding(inputs).transpose(1, 2)
        encoder_outputs = self.encoder.inference(embedded_inputs)
        mel_outputs, gate_outputs, alignments = self.decoder.inference(
            encoder_outputs)

        mel_outputs_postnet = self.postnet(mel_outputs)
        mel_outputs_postnet = mel_outputs + mel_outputs_postnet

        outputs = [mel_outputs, mel_outputs_postnet, gate_outputs, alignments]

        return outputs
