# coding=utf-8
# Adapted from
# https://github.com/huggingface/transformers/blob/a5cc30d72ae2dc19af534e4b35c986cc28db1275/src/transformers/models/falcon/modeling_falcon.py
# Copyright 2023 The vLLM team.
# Copyright 2023 the Falcon authors and HuggingFace Inc. team.  All rights
# reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""PyTorch Falcon model."""

import math
from typing import List, Optional, Tuple, Union

import torch
from torch import nn
from torch.nn import LayerNorm
from transformers import FalconConfig as HF_FalconConfig

from vllm.model_executor.input_metadata import InputMetadata
from vllm.model_executor.layers.activation import get_act_fn
from vllm.model_executor.layers.attention import PagedAttention
from vllm.model_executor.layers.linear import (ColumnParallelLinear,
                                               LinearMethodBase,
                                               QKVParallelLinear,
                                               RowParallelLinear)
from vllm.model_executor.layers.rotary_embedding import get_rope
from vllm.model_executor.layers.sampler import Sampler
from vllm.model_executor.layers.vocab_parallel_embedding import (
    VocabParallelEmbedding, ParallelLMHead)
from vllm.model_executor.parallel_utils.communication_op import (
    tensor_model_parallel_all_reduce)
from vllm.model_executor.parallel_utils.parallel_state import (
    get_tensor_model_parallel_rank, get_tensor_model_parallel_world_size)
from vllm.model_executor.sampling_metadata import SamplingMetadata
from vllm.model_executor.weight_utils import (default_weight_loader,
                                              hf_model_weights_iterator)
from vllm.sequence import SamplerOutput
from vllm.transformers_utils.configs import RWConfig

import json
from vllm._C import tensor_ops
import numpy as np
import contextlib
from vllm.model_executor.parallel_utils import cupy_utils
from vllm.model_executor.parallel_utils import custom_all_reduce

KVCache = List[torch.Tensor]

FalconConfig = Union[HF_FalconConfig, RWConfig]


def _get_alibi_slopes(total_num_heads: int) -> torch.Tensor:
    closest_power_of_2 = 2**math.floor(math.log2(total_num_heads))
    base = torch.tensor(2**(-(2**-(math.log2(closest_power_of_2) - 3))),
                        dtype=torch.float32)
    powers = torch.arange(1, 1 + closest_power_of_2, dtype=torch.int32)
    slopes = torch.pow(base, powers)

    if closest_power_of_2 != total_num_heads:
        extra_base = torch.tensor(
            2**(-(2**-(math.log2(2 * closest_power_of_2) - 3))),
            dtype=torch.float32)
        num_remaining_heads = min(closest_power_of_2,
                                  total_num_heads - closest_power_of_2)
        extra_powers = torch.arange(1,
                                    1 + 2 * num_remaining_heads,
                                    2,
                                    dtype=torch.int32)
        slopes = torch.cat(
            [slopes, torch.pow(extra_base, extra_powers)], dim=0)

    return slopes


class FalconAttention(nn.Module):

    def __init__(
        self,
        config: FalconConfig,
        linear_method: Optional[LinearMethodBase] = None,
    ):
        super().__init__()

        self.hidden_size = config.hidden_size
        tp_size = get_tensor_model_parallel_world_size()

        self.total_num_heads = config.num_attention_heads
        assert self.total_num_heads % tp_size == 0
        self.num_heads = self.total_num_heads // tp_size
        self.head_dim = self.hidden_size // self.total_num_heads
        assert self.head_dim * self.total_num_heads == self.hidden_size

        self.new_decoder_architecture = config.new_decoder_architecture
        self.multi_query = config.multi_query

        if self.new_decoder_architecture:
            self.total_num_kv_heads = config.num_kv_heads
        elif self.multi_query:
            self.total_num_kv_heads = 1
        else:
            self.total_num_kv_heads = self.total_num_heads
        if self.total_num_kv_heads >= tp_size:
            # Number of KV heads is greater than TP size, so we partition
            # the KV heads across multiple tensor parallel GPUs.
            assert self.total_num_kv_heads % tp_size == 0
        else:
            # Number of KV heads is less than TP size, so we replicate
            # the KV heads across multiple tensor parallel GPUs.
            assert tp_size % self.total_num_kv_heads == 0
        self.num_kv_heads = max(1, self.total_num_kv_heads // tp_size)

        self.query_key_value = QKVParallelLinear(
            self.hidden_size,
            self.head_dim,
            self.total_num_heads,
            self.total_num_kv_heads,
            bias=config.bias,
            skip_bias_add=True,
            linear_method=linear_method,
        )
        self.q_size = self.num_heads * self.head_dim
        self.kv_size = self.num_kv_heads * self.head_dim

        # Layer-wise attention scaling
        self.inv_norm_factor = 1.0 / math.sqrt(self.head_dim)
        self.reduce_row_parallel_results = not (config.new_decoder_architecture
                                                or config.parallel_attn)
        self.dense = RowParallelLinear(
            self.hidden_size,
            self.hidden_size,
            bias=config.bias,
            skip_bias_add=True,
            linear_method=linear_method,
            reduce_results=self.reduce_row_parallel_results)

        self.use_rotary = config.rotary
        self.use_alibi = config.alibi
        assert not (self.use_rotary and self.use_alibi), (
            "Rotary and alibi are mutually exclusive.")

        if self.use_rotary:
            rope_theta = getattr(config, "rope_theta", 10000)
            max_position_embeddings = getattr(config,
                                              "max_position_embeddings", 8192)
            self.rotary_emb = get_rope(
                self.head_dim,
                rotary_dim=self.head_dim,
                max_position=max_position_embeddings,
                base=rope_theta,
            )
            self.attn = PagedAttention(self.num_heads,
                                       self.head_dim,
                                       self.inv_norm_factor,
                                       num_kv_heads=self.num_kv_heads)
        elif self.use_alibi:
            tp_rank = get_tensor_model_parallel_rank()
            head_start = tp_rank * self.num_heads
            head_end = (tp_rank + 1) * self.num_heads
            alibi_slopes = (_get_alibi_slopes(self.total_num_heads) *
                            self.inv_norm_factor)
            alibi_slopes = alibi_slopes[head_start:head_end].tolist()
            self.attn = PagedAttention(self.num_heads,
                                       self.head_dim,
                                       self.inv_norm_factor,
                                       num_kv_heads=self.num_kv_heads,
                                       alibi_slopes=alibi_slopes)
        else:
            self.attn = PagedAttention(self.num_heads,
                                       self.head_dim,
                                       scale=self.inv_norm_factor,
                                       num_kv_heads=self.num_kv_heads)

    def forward(
        self,
        positions: torch.Tensor,
        hidden_states: torch.Tensor,
        kv_cache: KVCache,
        input_metadata: InputMetadata,
    ) -> torch.Tensor:
        qkv, bias = self.query_key_value(hidden_states)
        if bias is not None:
            qkv += bias
        q, k, v = qkv.split([self.q_size, self.kv_size, self.kv_size], dim=-1)
        if self.use_rotary:
            q, k = self.rotary_emb(positions, q, k)
        k_cache, v_cache = kv_cache
        attn_output = self.attn(q, k, v, k_cache, v_cache, input_metadata)
        attn_output, bias = self.dense(attn_output)
        return attn_output, bias


class FalconMLP(nn.Module):

    def __init__(
        self,
        config: FalconConfig,
        linear_method: Optional[LinearMethodBase] = None,
    ):
        super().__init__()
        hidden_size = config.hidden_size

        self.dense_h_to_4h = ColumnParallelLinear(hidden_size,
                                                  4 * hidden_size,
                                                  bias=config.bias,
                                                  skip_bias_add=True,
                                                  linear_method=linear_method)
        quant_config = getattr(linear_method, "quant_config", None)
        self.act = get_act_fn("gelu", quant_config, 4 * hidden_size)
        self.reduce_row_parallel_results = not (config.new_decoder_architecture
                                                or config.parallel_attn)
        self.dense_4h_to_h = RowParallelLinear(
            4 * hidden_size,
            hidden_size,
            bias=config.bias,
            skip_bias_add=True,
            reduce_results=self.reduce_row_parallel_results,
            linear_method=linear_method)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # NOTE(zhuohan): Following huggingface, we do not fuse bias add here.
        x, bias = self.dense_h_to_4h(x)
        if bias is not None:
            x += bias
        x = self.act(x)
        x, bias = self.dense_4h_to_h(x)
        return x, bias


class FalconDecoderLayer(nn.Module):

    def __init__(
        self,
        config: FalconConfig,
        linear_method: Optional[LinearMethodBase] = None,
    ):
        super().__init__()
        hidden_size = config.hidden_size
        self.num_heads = config.num_attention_heads
        self.self_attention = FalconAttention(config, linear_method)
        self.mlp = FalconMLP(config, linear_method)
        self.config = config

        if config.new_decoder_architecture:
            # The layer norm before self-attention
            self.ln_attn = LayerNorm(hidden_size,
                                     eps=config.layer_norm_epsilon)
            # The layer norm before the MLP
            self.ln_mlp = LayerNorm(hidden_size, eps=config.layer_norm_epsilon)
        else:
            self.input_layernorm = LayerNorm(hidden_size,
                                             eps=config.layer_norm_epsilon)
            if not config.parallel_attn:
                self.post_attention_layernorm = LayerNorm(
                    hidden_size, eps=config.layer_norm_epsilon)

        self.reduce_row_parallel_results = not (config.new_decoder_architecture
                                                or config.parallel_attn)

    def forward(
        self,
        positions: torch.Tensor,
        hidden_states: torch.Tensor,
        kv_cache: KVCache,
        input_metadata: InputMetadata,
    ) -> torch.Tensor:
        residual = hidden_states

        if self.config.new_decoder_architecture:
            attention_layernorm_out = self.ln_attn(hidden_states)
            mlp_layernorm_out = self.ln_mlp(hidden_states)
        else:
            attention_layernorm_out = self.input_layernorm(hidden_states)

        # Self attention.
        attention_output, attention_bias = self.self_attention(
            positions=positions,
            hidden_states=attention_layernorm_out,
            kv_cache=kv_cache,
            input_metadata=input_metadata,
        )
        if self.reduce_row_parallel_results and attention_bias is not None:
            attention_output += attention_bias

        if not self.config.new_decoder_architecture:
            if self.config.parallel_attn:
                mlp_layernorm_out = attention_layernorm_out
            else:
                residual += attention_output
                mlp_layernorm_out = self.post_attention_layernorm(residual)

        # MLP.
        mlp_output, mlp_bias = self.mlp(mlp_layernorm_out)
        if self.reduce_row_parallel_results and mlp_bias is not None:
            mlp_output += mlp_bias

        if not self.reduce_row_parallel_results:
            # When MLP and Attention layers are parallel, we can use
            # only one all-reduce operator to reduce the results from
            # both MLP and Attention layers.
            mlp_output += attention_output
            mlp_output = tensor_model_parallel_all_reduce(mlp_output)
            if attention_bias is not None:
                mlp_output += attention_bias
            if mlp_bias is not None:
                mlp_output += mlp_bias

        output = mlp_output + residual
        return output


class FalconModel(nn.Module):

    def __init__(
        self,
        config: FalconConfig,
        linear_method: Optional[LinearMethodBase] = None,
    ):
        super().__init__()
        self.config = config
        self.embed_dim = config.hidden_size
        self.num_heads = config.num_attention_heads
        self.use_alibi = config.alibi

        # Embedding + LN Embedding
        self.word_embeddings = VocabParallelEmbedding(
            config.vocab_size,
            self.embed_dim,
        )

        # Transformer blocks
        self.h = nn.ModuleList([
            FalconDecoderLayer(config, linear_method)
            for _ in range(config.num_hidden_layers)
        ])

        # Final Layer Norm
        self.ln_f = LayerNorm(self.embed_dim, eps=config.layer_norm_epsilon)

    def forward(
        self,
        input_ids: torch.LongTensor,
        positions: torch.Tensor,
        kv_caches: List[KVCache],
        input_metadata: InputMetadata,
        early_return: bool = False,
    ) -> torch.Tensor:
        hidden_states = self.word_embeddings(input_ids)
        for i in range(len(self.h)):
            layer = self.h[i]
            hidden_states = layer(
                positions,
                hidden_states,
                kv_caches[i],
                input_metadata,
            )
            # if change this, change result_hidden_states_addr as well
            if early_return:
                # turn off memory free inside the model before forward finish to avoid free GPU memory after end_capture
                print("early return......", flush=True)
                torch.cuda.memory_shutdown_free()
                return hidden_states
        hidden_states = self.ln_f(hidden_states)
        return hidden_states

@contextlib.contextmanager
def _maybe_cupy_nccl():
    if cupy_utils.is_initialized() and not custom_all_reduce.is_initialized():
        with with_cupy_nccl_for_all_reduce():
            yield
    else:
        yield

class FalconForCausalLM(nn.Module):

    def __init__(
        self,
        config: FalconConfig,
        linear_method: Optional[LinearMethodBase] = None,
        fast_start: bool = False,
        persist_cudagraph: bool = False,
        model_name: str = "",
        _BATCH_SIZES_TO_CAPTURE: List[int] = []
    ):
        super().__init__()
        self.config = config
        self.linear_method = linear_method
        self.transformer = FalconModel(config, linear_method)
        self.lm_head = ParallelLMHead(
            config.vocab_size,
            config.hidden_size,
        )
        self.sampler = Sampler(config.vocab_size)
        
        self.fast_start = fast_start
        self.persist_cudagraph = persist_cudagraph
        self.model_name = model_name
        self._BATCH_SIZES_TO_CAPTURE = _BATCH_SIZES_TO_CAPTURE

    def forward(
        self,
        input_ids: torch.LongTensor,
        positions: torch.Tensor,
        kv_caches: List[KVCache],
        input_metadata: InputMetadata,
        early_return: bool = False,
    ) -> torch.Tensor:
        hidden_states = self.transformer(
            input_ids,
            positions,
            kv_caches,
            input_metadata,
            early_return = early_return
        )
        return hidden_states

    def sample(
        self,
        hidden_states: torch.Tensor,
        sampling_metadata: SamplingMetadata,
    ) -> Optional[SamplerOutput]:
        next_tokens = self.sampler(self.lm_head.weight, hidden_states,
                                   sampling_metadata)
        return next_tokens

    def load_weights(self,
                     model_name_or_path: str,
                     cache_dir: Optional[str] = None,
                     load_format: str = "auto",
                     revision: Optional[str] = None,
                     async_load: bool = False,
                     save_tensor: bool = False,
                     gpu_cache: List[KVCache] = []):
        total_num_heads = self.config.num_attention_heads
        if self.config.new_decoder_architecture:
            total_num_kv_heads = self.config.num_kv_heads
        elif self.config.multi_query:
            total_num_kv_heads = 1
        else:
            total_num_kv_heads = total_num_heads
        num_query_heads_per_kv_head = total_num_heads // total_num_kv_heads
        if save_tensor:
            params_dict = dict(self.named_parameters())
            shape_dict = {}
            for name, loaded_weight in hf_model_weights_iterator(
                    model_name_or_path, cache_dir, load_format, revision):
                # Skip loading extra bias for GPTQ models.
                if name.endswith(".bias") and name not in params_dict:
                    continue
                param = params_dict[name]
                if "query_key_value" in name:
                    output_dim = getattr(param, "output_dim", None)
                    loaded_weight_shape = loaded_weight.shape
                    if output_dim is not None:
                        loaded_weight = loaded_weight.view(
                            loaded_weight_shape[:output_dim] +
                            (total_num_kv_heads, num_query_heads_per_kv_head + 2,
                            -1) + loaded_weight_shape[output_dim + 1:])
                        wq = loaded_weight.narrow(
                            output_dim + 1, 0,
                            num_query_heads_per_kv_head).reshape(
                                *loaded_weight_shape[:output_dim], -1,
                                *loaded_weight_shape[output_dim + 1:])
                        wk = loaded_weight.narrow(
                            output_dim + 1, num_query_heads_per_kv_head,
                            1).reshape(*loaded_weight_shape[:output_dim], -1,
                                    *loaded_weight_shape[output_dim + 1:])
                        wv = loaded_weight.narrow(
                            output_dim + 1, num_query_heads_per_kv_head + 1,
                            1).reshape(*loaded_weight_shape[:output_dim], -1,
                                    *loaded_weight_shape[output_dim + 1:])
                        loaded_weight = torch.cat([wq, wk, wv], dim=output_dim)

                weight_loader = getattr(param, "weight_loader",
                                        default_weight_loader)
                weight_loader(param, loaded_weight)
                shape_dict[name] = param.shape
                tensor_ops.save_tensor_prepare(param.cpu(), f"/home/zsx/raidfs-back/tensors/{self.model_name}/{name}")
            tensor_ops.save_tensor_start()
            json_data = json.dumps(shape_dict)
            with open(f"/home/zsx/raidfs-back/tensors/{self.model_name}/shape_dict.json", "w") as f:
                f.write(json_data)
        else:
            # if not async_load:
                # params_dict = dict(self.named_parameters())
                # for name, loaded_weight in hf_model_weights_iterator(
                #         model_name_or_path, cache_dir, load_format, revision):
                #     # Skip loading extra bias for GPTQ models.
                #     if name.endswith(".bias") and name not in params_dict:
                #         continue
                #     param = params_dict[name]
                #     if "query_key_value" in name:
                #         output_dim = getattr(param, "output_dim", None)
                #         loaded_weight_shape = loaded_weight.shape
                #         if output_dim is not None:
                #             loaded_weight = loaded_weight.view(
                #                 loaded_weight_shape[:output_dim] +
                #                 (total_num_kv_heads, num_query_heads_per_kv_head + 2,
                #                 -1) + loaded_weight_shape[output_dim + 1:])
                #             wq = loaded_weight.narrow(
                #                 output_dim + 1, 0,
                #                 num_query_heads_per_kv_head).reshape(
                #                     *loaded_weight_shape[:output_dim], -1,
                #                     *loaded_weight_shape[output_dim + 1:])
                #             wk = loaded_weight.narrow(
                #                 output_dim + 1, num_query_heads_per_kv_head,
                #                 1).reshape(*loaded_weight_shape[:output_dim], -1,
                #                         *loaded_weight_shape[output_dim + 1:])
                #             wv = loaded_weight.narrow(
                #                 output_dim + 1, num_query_heads_per_kv_head + 1,
                #                 1).reshape(*loaded_weight_shape[:output_dim], -1,
                #                         *loaded_weight_shape[output_dim + 1:])
                #             loaded_weight = torch.cat([wq, wk, wv], dim=output_dim)

                #     weight_loader = getattr(param, "weight_loader",
                #                             default_weight_loader)
                #     weight_loader(param, loaded_weight)
            params_dict = dict(self.named_parameters())
            shape_dict = {}
            with open(f"/home/zsx/raidfs-back/tensors/{self.model_name}/shape_dict.json", "r") as file:
                shape_dict = json.load(file)
            for name, loaded_weight_shape in shape_dict.items():
                # Skip loading extra bias for GPTQ models.
                if name.endswith(".bias") and name not in params_dict:
                    continue
                param = params_dict[name]
                tensor_ops.load_tensor_async_prepare(param, f"/home/zsx/raidfs-back/tensors/{self.model_name}/{name}")
            tensor_ops.load_tensor_async_start()
            if not async_load:
                tensor_ops.load_tensor_sync_all()
                
        if self.persist_cudagraph:
            # warmup here, tigger async module load
            _PAD_SLOT_ID = -1
            max_batch_size = max(self._BATCH_SIZES_TO_CAPTURE)
            input_tokens = torch.zeros(max_batch_size, 1, dtype=torch.long).cuda()
            input_positions = torch.zeros(max_batch_size, 1,
                                        dtype=torch.long).cuda()
            slot_mapping = torch.empty(max_batch_size, 1, dtype=torch.long).cuda()
            slot_mapping.fill_(_PAD_SLOT_ID)
            context_lens = torch.ones(max_batch_size, dtype=torch.int32).cuda()
            graph_block_tables = np.zeros(
                (max(self._BATCH_SIZES_TO_CAPTURE), 256), dtype=np.int32)
            block_tables = torch.from_numpy(graph_block_tables).cuda()
 
            s = torch.cuda.Stream()
            with torch.cuda.stream(s):
                for bs in self._BATCH_SIZES_TO_CAPTURE:
                    input_metadata = InputMetadata(
                        is_prompt=False,
                        slot_mapping=slot_mapping[:bs],
                        prompt_lens=None,
                        max_seq_len=None,
                        start_loc=None,
                        max_context_len=1024,
                        context_lens=context_lens[:bs],
                        block_tables=block_tables[:bs],
                        use_cuda_graph=True,
                        kv_cache_dtype="auto",
                    )
                    with _maybe_cupy_nccl():
                        self.forward(
                            input_tokens[:bs],
                            input_positions[:bs],
                            gpu_cache,
                            input_metadata,
                            early_return = True,
                        )
                    # pair with early_return = True
                    torch.cuda.memory_turnon_free()
                # not synchronize, let lazy module load async
                # torch.cuda.synchronize()
