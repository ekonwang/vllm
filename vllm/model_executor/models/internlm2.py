# -*- coding: utf-8 -*-
from typing import Any, Dict, List, Optional, Tuple

import torch
from torch import nn
from transformers import PretrainedConfig

from vllm.attention import Attention, AttentionMetadata
from vllm.model_executor.layers.activation import SiluAndMul
from vllm.model_executor.layers.layernorm import RMSNorm
from vllm.model_executor.layers.linear import (LinearMethodBase,
                                               MergedColumnParallelLinear,
                                               QKVParallelLinear,
                                               RowParallelLinear)
from vllm.model_executor.layers.logits_processor import LogitsProcessor
from vllm.model_executor.layers.rotary_embedding import get_rope
from vllm.model_executor.layers.sampler import Sampler, MockSampler
from vllm.model_executor.layers.vocab_parallel_embedding import (
    ParallelLMHead, VocabParallelEmbedding)
from vllm.model_executor.parallel_utils.parallel_state import (
    get_tensor_model_parallel_world_size)
from vllm.model_executor.sampling_metadata import SamplingMetadata
from vllm.model_executor.weight_utils import (default_weight_loader,
                                              hf_model_weights_iterator)
from vllm.sequence import SamplerOutput


class InternLM2MLP(nn.Module):

    def __init__(
        self,
        hidden_size: int,
        intermediate_size: int,
        hidden_act: str,
        linear_method: Optional[LinearMethodBase] = None,
    ) -> None:
        super().__init__()
        self.gate_up_proj = MergedColumnParallelLinear(
            hidden_size, [intermediate_size] * 2,
            bias=False,
            linear_method=linear_method)
        self.w2 = RowParallelLinear(intermediate_size,
                                    hidden_size,
                                    bias=False,
                                    linear_method=linear_method)
        if hidden_act != "silu":
            raise ValueError(f"Unsupported activation: {hidden_act}. "
                             "Only silu is supported for now.")
        self.act_fn = SiluAndMul()

    def forward(self, x):
        gate_up, _ = self.gate_up_proj(x)
        x = self.act_fn(gate_up)
        x, _ = self.w2(x)
        return x


class InternLM2Attention(nn.Module):

    def __init__(
        self,
        hidden_size: int,
        num_heads: int,
        num_kv_heads: int,
        rope_theta: float = 10000,
        rope_scaling: Optional[Dict[str, Any]] = None,
        max_position_embeddings: int = 8192,
        linear_method: Optional[LinearMethodBase] = None,
    ) -> None:
        super().__init__()
        self.hidden_size = hidden_size
        tp_size = get_tensor_model_parallel_world_size()
        self.total_num_heads = num_heads
        assert self.total_num_heads % tp_size == 0
        self.num_heads = self.total_num_heads // tp_size
        self.total_num_kv_heads = num_kv_heads
        if self.total_num_kv_heads >= tp_size:
            # Number of KV heads is greater than TP size, so we partition
            # the KV heads across multiple tensor parallel GPUs.
            assert self.total_num_kv_heads % tp_size == 0
        else:
            # Number of KV heads is less than TP size, so we replicate
            # the KV heads across multiple tensor parallel GPUs.
            assert tp_size % self.total_num_kv_heads == 0
        self.num_kv_heads = max(1, self.total_num_kv_heads // tp_size)
        self.head_dim = hidden_size // self.total_num_heads
        self.q_size = self.num_heads * self.head_dim
        self.kv_size = self.num_kv_heads * self.head_dim
        self.scaling = self.head_dim**-0.5
        self.rope_theta = rope_theta
        self.max_position_embeddings = max_position_embeddings

        self.wqkv = QKVParallelLinear(
            hidden_size,
            self.head_dim,
            self.total_num_heads,
            self.total_num_kv_heads,
            bias=False,
            linear_method=linear_method,
        )
        self.wo = RowParallelLinear(
            self.total_num_heads * self.head_dim,
            hidden_size,
            bias=False,
            linear_method=linear_method,
        )

        self.rotary_emb = get_rope(
            self.head_dim,
            rotary_dim=self.head_dim,
            max_position=max_position_embeddings,
            base=rope_theta,
            rope_scaling=rope_scaling,
        )
        self.attn = Attention(self.num_heads,
                              self.head_dim,
                              self.scaling,
                              num_kv_heads=self.num_kv_heads)

    def forward(
        self,
        positions: torch.Tensor,
        hidden_states: torch.Tensor,
        kv_cache: torch.Tensor,
        attn_metadata: AttentionMetadata,
    ) -> torch.Tensor:
        qkv, _ = self.wqkv(hidden_states)
        q, k, v = qkv.split([self.q_size, self.kv_size, self.kv_size], dim=-1)
        q, k = self.rotary_emb(positions, q, k)
        attn_output = self.attn(q, k, v, kv_cache, attn_metadata)
        output, _ = self.wo(attn_output)
        return output


class InternLMDecoderLayer(nn.Module):

    def __init__(
        self,
        config: PretrainedConfig,
        linear_method: Optional[LinearMethodBase] = None,
    ) -> None:
        super().__init__()
        self.hidden_size = config.hidden_size
        rope_theta = getattr(config, "rope_theta", 10000)
        rope_scaling = getattr(config, "rope_scaling", None)
        max_position_embeddings = getattr(config, "max_position_embeddings",
                                          8192)
        self.attention = InternLM2Attention(
            hidden_size=self.hidden_size,
            num_heads=config.num_attention_heads,
            num_kv_heads=config.num_key_value_heads,
            rope_theta=rope_theta,
            rope_scaling=rope_scaling,
            max_position_embeddings=max_position_embeddings,
            linear_method=linear_method,
        )
        self.feed_forward = InternLM2MLP(
            hidden_size=self.hidden_size,
            intermediate_size=config.intermediate_size,
            hidden_act=config.hidden_act,
            linear_method=linear_method,
        )
        self.attention_norm = RMSNorm(config.hidden_size,
                                      eps=config.rms_norm_eps)
        self.ffn_norm = RMSNorm(config.hidden_size, eps=config.rms_norm_eps)

    def forward(
        self,
        positions: torch.Tensor,
        hidden_states: torch.Tensor,
        kv_cache: torch.Tensor,
        attn_metadata: AttentionMetadata,
        residual: Optional[torch.Tensor],
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        # Self Attention
        if residual is None:
            residual = hidden_states
            hidden_states = self.attention_norm(hidden_states)
        else:
            hidden_states, residual = self.attention_norm(
                hidden_states, residual)
        hidden_states = self.attention(
            positions=positions,
            hidden_states=hidden_states,
            kv_cache=kv_cache,
            attn_metadata=attn_metadata,
        )

        # Fully Connected
        hidden_states, residual = self.ffn_norm(hidden_states, residual)
        hidden_states = self.feed_forward(hidden_states)
        return hidden_states, residual


class InternLM2Model(nn.Module):

    def __init__(
        self,
        config: PretrainedConfig,
        linear_method: Optional[LinearMethodBase] = None,
    ) -> None:
        super().__init__()
        self.config = config
        self.padding_idx = config.pad_token_id
        self.vocab_size = config.vocab_size
        self.tok_embeddings = VocabParallelEmbedding(
            config.vocab_size,
            config.hidden_size,
        )
        self.layers = nn.ModuleList([
            InternLMDecoderLayer(config, linear_method)
            for _ in range(config.num_hidden_layers)
        ])
        self.norm = RMSNorm(config.hidden_size, eps=config.rms_norm_eps)
        self.DEBUG_START = False

    def forward(
        self,
        input_ids: torch.Tensor,
        positions: torch.Tensor,
        kv_caches: List[torch.Tensor],
        attn_metadata: AttentionMetadata,
    ) -> torch.Tensor:
        hidden_states = self.tok_embeddings(input_ids)
        residual = None
        for i in range(len(self.layers)):
            layer = self.layers[i]
            hidden_states, residual = layer(
                positions,
                hidden_states,
                kv_caches[i],
                attn_metadata,
                residual,
            )
        hidden_states, _ = self.norm(hidden_states, residual)
        return hidden_states


class InternLM2ForCausalLM(nn.Module):

    def __init__(
        self,
        config: PretrainedConfig,
        linear_method: Optional[LinearMethodBase] = None,
    ) -> None:
        super().__init__()
        self.config = config
        self.linear_method = linear_method
        self.model = InternLM2Model(config, linear_method)
        self.output = ParallelLMHead(config.vocab_size, config.hidden_size)
        self.logits_processor = LogitsProcessor(config.vocab_size)
        self.sampler = Sampler()
        self.DEBUG_START = False

    def forward(
        self,
        input_ids: torch.Tensor,
        positions: torch.Tensor,
        kv_caches: List[torch.Tensor],
        attn_metadata: AttentionMetadata,
    ) -> torch.Tensor:
        hidden_states = self.model(input_ids, positions, kv_caches,
                                   attn_metadata)
        return hidden_states

    def compute_logits(self, hidden_states: torch.Tensor,
                       sampling_metadata: SamplingMetadata) -> torch.Tensor:
        logits = self.logits_processor(self.output.weight, hidden_states,
                                       sampling_metadata)
        # --- @YIKUN: 4.18 for InternLM Preference vllm acceleration (IPVA project) debugging --- #
        # if self.DEBUG_START:
        #     # import pdb; pdb.set_trace()
        return logits

    def sample(
        self,
        logits: torch.Tensor,
        sampling_metadata: SamplingMetadata,
    ) -> Optional[SamplerOutput]:
        next_tokens = self.sampler(logits, sampling_metadata)
        # --- 4.18 for InternLM Preference vllm acceleration (IPVA project) debugging --- #
        # if self.DEBUG_START:
        #     # import pdb; pdb.set_trace()
        return next_tokens

    def load_weights(self,
                     model_name_or_path: str,
                     cache_dir: Optional[str] = None,
                     load_format: str = "auto",
                     revision: Optional[str] = None):
        stacked_params_mapping = [
            # (param_name, shard_name, shard_id)
            ("gate_up_proj", "w1", 0),
            ("gate_up_proj", "w3", 1),
        ]
        params_dict = dict(self.named_parameters())
        for name, loaded_weight in hf_model_weights_iterator(
                model_name_or_path, cache_dir, load_format, revision):
            if "rotary_emb.inv_freq" in name:
                continue
            for (param_name, weight_name, shard_id) in stacked_params_mapping:
                if weight_name not in name:
                    continue
                name = name.replace(weight_name, param_name)
                # Skip loading extra bias for GPTQ models.
                if name.endswith(".bias") and name not in params_dict:
                    continue
                param = params_dict[name]
                weight_loader = param.weight_loader
                weight_loader(param, loaded_weight, shard_id)
                break
            else:
                # Skip loading extra bias for GPTQ models.
                if name.endswith(".bias") and name not in params_dict:
                    continue
                param = params_dict[name]
                if "wqkv" in name:
                    config = self.config
                    kv_groups = (config.num_attention_heads //
                                 config.num_key_value_heads)
                    head_dim = config.hidden_size // config.num_attention_heads
                    loaded_weight = loaded_weight.view(-1, 2 + kv_groups,
                                                       head_dim,
                                                       loaded_weight.shape[-1])
                    wq, wk, wv = torch.split(loaded_weight, [kv_groups, 1, 1],
                                             dim=1)
                    wq = wq.reshape(-1, wq.shape[-1])
                    wk = wk.reshape(-1, wk.shape[-1])
                    wv = wv.reshape(-1, wv.shape[-1])
                    weight_loader = param.weight_loader
                    weight_loader(param, wq, 'q')
                    weight_loader(param, wk, 'k')
                    weight_loader(param, wv, 'v')
                else:
                    weight_loader = getattr(param, "weight_loader",
                                            default_weight_loader)
                    weight_loader(param, loaded_weight)


class InternLM2ForSequenceClassification(nn.Module):

    def __init__(
        self,
        config: PretrainedConfig,
        linear_method: Optional[LinearMethodBase] = None,
    ) -> None:
        super().__init__()
        self.config = config
        self.linear_method = linear_method
        self.model = InternLM2Model(config, linear_method)
        # self.score = ParallelLMHead(1, config.hidden_size)
        self.score = nn.Linear(config.hidden_size, 1, bias=False)
        self.logits_processor = LogitsProcessor(1)
        self.sampler = MockSampler()
        self.preference_max_length = config.preference_max_length
        self.DEBUG_START = False

    def forward(
        self,
        input_ids: torch.Tensor,
        positions: torch.Tensor,
        kv_caches: List[torch.Tensor],
        attn_metadata: AttentionMetadata,
    ) -> torch.Tensor:
        # TODO: the `input_ids` seems to be wrong. 
        hidden_states = self.model(input_ids, positions, kv_caches,
                                   attn_metadata)
        self.subquery_start_loc = attn_metadata.subquery_start_loc
        self.prompt_lens = attn_metadata.prompt_lens
        if isinstance(self.prompt_lens, list):
            self.prompt_lens = torch.tensor(self.prompt_lens, device=hidden_states.device, dtype=torch.long)
            self.prompt_lens = torch.clip(self.prompt_lens, 0, self.preference_max_length)
        else:
            self.prompt_lens = None
        return hidden_states

    def compute_logits(self, hidden_states: torch.Tensor,
                       sampling_metadata: SamplingMetadata) -> torch.Tensor:
        if (self.prompt_lens is not None) and self.prompt_lens.dtype == torch.long:
            self.last_selected_idxs = self.subquery_start_loc[:-1] + self.prompt_lens - 1
            selected_hidden_states = hidden_states[self.last_selected_idxs]
            self.prompt_lens = None 
        else:
            # mock run
            selected_hidden_states = hidden_states[:20]

        logits = self.score(selected_hidden_states)
        if self.DEBUG_START:
            import pdb; pdb.set_trace()
        return logits

    def sample(
        self,
        logits: torch.Tensor,
        sampling_metadata: SamplingMetadata,
    ) -> Optional[SamplerOutput]:
        # @YIKUN: 直接返回 tensor 导致 vllm 任务无法结束。
        # return logits
        # 简单用伪 Sampler 进行封装，避免 vllm 任务队列认为任务未结束
        logits_res = logits.cpu().tolist()
        return logits_res, self.sampler(logits, sampling_metadata)

    def load_weights(self,
                     model_name_or_path: str,
                     cache_dir: Optional[str] = None,
                     load_format: str = "auto",
                     revision: Optional[str] = None):
        stacked_params_mapping = [
            # (param_name, shard_name, shard_id)
            ("gate_up_proj", "w1", 0),
            ("gate_up_proj", "w3", 1),
        ]
        params_dict = dict(self.named_parameters())
        for name, loaded_weight in hf_model_weights_iterator(
                model_name_or_path, cache_dir, load_format, revision):
            if "rotary_emb.inv_freq" in name:
                continue
            for (param_name, weight_name, shard_id) in stacked_params_mapping:
                if weight_name not in name:
                    continue
                name = name.replace(weight_name, param_name)
                # Skip loading extra bias for GPTQ models.
                if name.endswith(".bias") and name not in params_dict:
                    continue
                param = params_dict[name]
                weight_loader = param.weight_loader
                weight_loader(param, loaded_weight, shard_id)
                break
            else:
                # Skip loading extra bias for GPTQ models.
                if name.endswith(".bias") and name not in params_dict:
                    continue
                param = params_dict[name]
                if "wqkv" in name:
                    config = self.config
                    kv_groups = (config.num_attention_heads //
                                 config.num_key_value_heads)
                    head_dim = config.hidden_size // config.num_attention_heads
                    loaded_weight = loaded_weight.view(-1, 2 + kv_groups,
                                                       head_dim,
                                                       loaded_weight.shape[-1])
                    wq, wk, wv = torch.split(loaded_weight, [kv_groups, 1, 1],
                                             dim=1)
                    wq = wq.reshape(-1, wq.shape[-1])
                    wk = wk.reshape(-1, wk.shape[-1])
                    wv = wv.reshape(-1, wv.shape[-1])
                    weight_loader = param.weight_loader
                    weight_loader(param, wq, 'q')
                    weight_loader(param, wk, 'k')
                    weight_loader(param, wv, 'v')
                else:
                    weight_loader = getattr(param, "weight_loader",
                                            default_weight_loader)
                    weight_loader(param, loaded_weight)
