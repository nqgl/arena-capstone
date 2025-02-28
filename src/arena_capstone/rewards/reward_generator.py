from arena_capstone.rlhf_trojan_competition.src.models.reward_model import (
    RewardModel,
    RewardModelOutput,
)
from arena_capstone.algorithm.embedding_model import (
    TokensBatch,
    EmbeddedBatch,
    EmbeddingFriendlyForCausalLM,
)

import os

from typing import List, Union
from jaxtyping import Int, Float

import torch
import torch.nn.functional as F
from torch import Tensor


class RewardGenerator(RewardModel):
    def __init__(self, *args, softmax=F.softmax, **kwargs):
        super().__init__(*args, **kwargs)
        self.embedding_model = EmbeddingFriendlyForCausalLM(self)
        self.softmax = softmax

    def forward(  # pylint: disable=too-many-argument
        self,
        attention_mask: torch.Tensor,
        input_ids: torch.LongTensor = None,
        position_ids: torch.LongTensor = None,
        past_key_values: list[torch.FloatTensor] = None,
        inputs_embeds: torch.FloatTensor = None,
        use_cache: bool = None,
        output_attentions: bool = None,
        output_hidden_states: bool = None,
        return_dict: bool = None,
    ) -> Union[tuple[torch.Tensor, torch.Tensor], RewardModelOutput]:
        """
        Args:

        Returns:

        Examples:

        ```python
        >>> from src.models import RewardModel
        >>> from transformers import LlamaTokenizer

        >>> import torch
        >>> device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        >>> model = LlamaForCausalLM.from_pretrained(PATH_TO_MODEL).to(device)
        >>> tokenizer = AutoTokenizer.from_pretrained(PATH_TO_MODEL)

        >>> prompt = "Hey, are you conscious? Can you talk to me?"
        >>> inputs = tokenizer(prompt, return_tensors="pt").to(device)

        # got reward
        >>> outputs = model(**inputs)
        >>> reward = outputs.end_rewards
        >>> reward
        tensor([[[0.0000]]]) # Reward will not be 0 but an arbitrary float
        ```
        """
        assert attention_mask is not None
        output_attentions = (
            output_attentions
            if output_attentions is not None
            else self.config.output_attentions
        )
        output_hidden_states = (
            output_hidden_states
            if output_hidden_states is not None
            else self.config.output_hidden_states
        )
        return_dict = (
            return_dict if return_dict is not None else self.config.use_return_dict
        )

        if input_ids is not None:
            outputs = self.model(
                input_ids=input_ids,
                attention_mask=attention_mask,
                position_ids=position_ids,
                past_key_values=past_key_values,
                inputs_embeds=inputs_embeds,
                use_cache=use_cache,
                output_attentions=output_attentions,
                output_hidden_states=output_hidden_states,
                return_dict=return_dict,
            )
            hidden_states = outputs[0]  # size = (B, L, E)
            rewards = self.score_head(hidden_states)  # size = (B, L, D)

            end_rewards = []
            for i in range(input_ids.size(0)):
                end_index = attention_mask[i].nonzero()[-1].item()
                end_rewards.append(rewards[i, end_index])  # size = (D,)
            end_rewards = torch.stack(end_rewards, dim=0)  # size = (B, D)

            if not return_dict:
                return rewards, end_rewards

            reward_out = RewardModelOutput(
                rewards=rewards,  # size = (B, L, D)
                end_rewards=end_rewards,  # size = (B, D)
            )

        else:
            assert inputs_embeds is not None
            outputs = self.model(
                attention_mask=attention_mask,
                position_ids=position_ids,
                past_key_values=past_key_values,
                inputs_embeds=inputs_embeds,
                use_cache=use_cache,
                output_attentions=output_attentions,
                output_hidden_states=output_hidden_states,
                return_dict=return_dict,
            )

            hidden_states = outputs[0]  # size = (B, L, E)
            rewards = self.score_head(hidden_states)  # size = (B, L, D)

            end_rewards = []
            for i in range(inputs_embeds.size(0)):
                end_index = attention_mask[i].nonzero()[-1].item()
                end_rewards.append(rewards[i, end_index])  # size = (D,)
            end_rewards = torch.stack(end_rewards, dim=0)  # size = (B, D)

            if not return_dict:
                return rewards, end_rewards

            reward_out = RewardModelOutput(
                rewards=rewards,  # size = (B, L, D)
                end_rewards=end_rewards,  # size = (B, D)
            )
        setattr(reward_out, "attention_mask", attention_mask)
        return reward_out

    def logit_rewards_from_embedded_batch(
        self,
        # batch: EmbeddedBatch,
        reward_batch: EmbeddedBatch,
        target_logits=None,
        div_target_logits=False,
        temperature=1,
    ):
        # assert batch.logits is not None

        target_logits = (
            target_logits
            or reward_batch.logits[:, :-1][reward_batch.target_mask[:, 1:]]
        )
        if div_target_logits:
            target_logits = target_logits / div_target_logits
        new_embed = reward_batch.embeddings
        flat_embedded_logits = self.embedding_model.embed(
            self.softmax(target_logits / temperature, dim=-1),
            onehot=True,
        )
        if torch.any(torch.isinf(flat_embedded_logits)):
            print("inf in flat_embedded_logits")

        if torch.any(torch.isinf(new_embed)):
            print("inf in new_embed")
        newer_embed = torch.masked_scatter(
            new_embed[:, 1:],
            reward_batch.target_mask[:, 1:].unsqueeze(-1),
            flat_embedded_logits,
        )
        newer_embed = torch.cat([new_embed[:, :1], newer_embed], dim=1)
        if torch.any(torch.isinf(newer_embed)):
            print("inf in newer_embed")

        # new_embed[batch.target_mask]
        print("newer_embed_shape", newer_embed.shape)
        reward_output = self(
            input_ids=None,
            attention_mask=torch.ones(newer_embed.shape[:2], device="cuda"),
            inputs_embeds=newer_embed,
        )
        return reward_output

        # newer_embed = torch.scatter(
        #     new_embed,
        #     0,
        #     index = mask_indices,
        #     src = flat_embedded_logits
        # )
        # target 7, 10
        # final rew should come from:
        # token 0, 1, 2, ...,7
        # + logits 7, 8, 9, 10
        # target is 4,5,6
        # logits care about 3 4 5 (6)
        # tokens care about 0 1 2 3
        # mask = zeros(7)
        # start = 4, end = 7
        # mask[4:7] = 1
        # mask: "4 5 6"
        # mask[1:] : "3 4 5"
        # logits[:-1] : "0 1 2 3 4 5"
        # logits[:-1][mask[1:]] : "3 4 5"
        # tokens[:4] : "0 1 2 3"

    def logit_rewards_from_tokens_batch(
        self,
        batch: TokensBatch,
        div_target_logits=False,
        temperature=1,
    ):
        """
        Calculates and returns rewards over across batches for all target logits (all at once), from a TokensBatch.
        NO GRAD: Cannot backward through these reward values!
        """
        assert batch.logits is not None
        with torch.inference_mode():
            # target_start
            target_start, target_end = batch.target_bounds
            # low = target_start
            # high = target_end - 1
            # we do take the last logit here, because we care about all targets
            # nvm that was wrong I think it's just
            # low, high = batch.target_bounds
            target_logits = batch.logits[:, target_start - 1 : target_end - 1]
            if div_target_logits:
                target_logits = target_logits / div_target_logits
            embedded_tokens = self.embedding_model.embed(batch.tokens[:, :target_start])
            embedded_logits = self.embedding_model.embed(
                self.softmax(target_logits / temperature, dim=-1),
                onehot=True,
            )
            embedded = torch.cat(
                [embedded_tokens.squeeze(0), embedded_logits.squeeze(0)], dim=1
            )
            reward_output = self(
                input_ids=None,
                attention_mask=torch.ones(embedded.shape[:2], device="cuda"),
                inputs_embeds=embedded,
            )
        return reward_output

    def logit_rewards_loop_over_tokens_batch(
        self,
        batch: TokensBatch,
        div_target_logits=False,
        temperature=1,
    ):
        """
        NO GRAD NEEDED FROM THIS
        or provided ;)
        """
        assert batch.logits is not None
        target_start, target_end = batch.target_bounds
        target_length = target_end - target_start
        rewards = torch.zeros(
            batch.tokens.shape[0],
            target_length,
            1,
            device="cuda",
        )
        with torch.inference_mode():
            # target_start
            # low = target_start
            # high = target_end - 1
            # we do take the last logit here, because we care about all targets
            # nvm that was wrong I think it's just
            # low, high = batch.target_bounds
            for target_logit_index in range(target_start, target_end):
                # target_logit_index = target_start + target_logit_relative_index
                target_logits = batch.logits[
                    :, target_logit_index - 1 : target_logit_index
                ]
                if div_target_logits:
                    target_logits = target_logits / div_target_logits
                embedded_tokens = self.embedding_model.embed(
                    batch.tokens[:, :target_logit_index]
                )
                embedded_logits = self.embedding_model.embed(
                    self.softmax(target_logits / temperature, dim=-1),
                    onehot=True,
                )
                embedded = torch.cat(
                    [embedded_tokens.squeeze(0), embedded_logits.squeeze(0)], dim=1
                )
                reward_output = self(
                    input_ids=None,
                    attention_mask=torch.ones(embedded.shape[:2], device="cuda"),
                    inputs_embeds=embedded,
                )
                rewards[:, target_logit_index - target_start] = (
                    reward_output.end_rewards
                )
        return rewards

        # [P][S][PS]logits([tttttttt])

        # model -> logits([t])
        # [P][S][PS][]logits([t])  ->reward``
        # [P][S][PS][t]logits([t])  ->reward``
        # [P][S][PS][tt]logits([t])  ->reward``
        # [P][S][PS][ttt]logits([t])  ->reward```

    # def logit_rewards_loop_over_embedded_batch(
    #     self,
    #     batch: EmbeddedBatch,
    #     reward_batch: EmbeddedBatch,
    #     div_target_logits=False,
    # ):
    #     assert batch.logits is not None
    #     target_logits = batch.logits[:, :-1][batch.target_mask[:, 1:]]
    #     if div_target_logits:
    #         target_logits = target_logits / div_target_logits
    #     new_embed = reward_batch.embeddings

    #     flat_embedded_logits = self.embedding_model.embed(
    #         self.softmax(target_logits, dim=-1),
    #         onehot=True,
    #     )
    #     if flat_embedded_logits.dtype != torch.float16:
    #         print("flat_embedded_logits is not float16")
    #         flat_embedded_logits = flat_embedded_logits

    #     if torch.any(torch.isinf(flat_embedded_logits)):
    #         print("inf in flat_embedded_logits")

    #     if torch.any(torch.isinf(new_embed)):
    #         print("inf in new_embed")
    #     newer_embed = torch.masked_scatter(
    #         new_embed[:, 1:],
    #         reward_batch.target_mask[:, 1:].unsqueeze(-1),
    #         flat_embedded_logits,
    #     )
    #     assert batch.logits is not None

    #     target_logits = batch.logits[:, :-1][batch.target_mask[:, 1:]]
    #     if div_target_logits:
    #         target_logits = target_logits / div_target_logits
    #     newer_embed = torch.cat([new_embed[:, :1], newer_embed], dim=1)
    #     if torch.any(torch.isinf(newer_embed)):
    #         print("inf in newer_embed")

    #     # new_embed[batch.target_mask]
    #     print("newer_embed_shape", newer_embed.shape)
    #     reward_output = self(
    #         input_ids=None,
    #         attention_mask=torch.ones(newer_embed.shape[:2], device="cuda"),
    #         inputs_embeds=newer_embed,
    #     )

    #     # /        return reward_output

    #     for target_logit_index in range(target_start, target_end):
    #         xbatch = batch.copy()
    #         xbatch.target_mask = torch.zeros()
    #         xbatch.target_mask[:, target_logit_index] = 1
    #         self.logit_rewards_from_embedded_batch(xbatch)

    ## What happens in reward_upo

    def logits_loop_embed(
        self,
        prefixes,
        suffix,
        post_suffix,
        targets,
        base_embedding_model,
        div_target_logits=False,
        temperature=1,
    ):
        prefix = prefixes[0]
        target = targets[0]
        long_ps = torch.cat([post_suffix, target])

        base_grad_batch_long = base_embedding_model.splice_embedded_batch(
            prefixes=[prefix],
            suffix_tokens=suffix,
            post_suffix_tokens=long_ps,
            targets=[target[0:0]],
            get_logits=False,
        )
        base_grad_batch_short = base_embedding_model.splice_embedded_batch(
            prefixes=[prefix],
            suffix_tokens=suffix,
            post_suffix_tokens=post_suffix,
            targets=[target],
            get_logits=True,
        )

        # loop_batch = EmbeddedBatch(
        #     # logits=base_grad_batch_short.logits,
        #     embeddings=base_grad_batch_short.embeddings,
        #     target_mask=base_grad_batch_short.target_mask,
        #     suffix_tensor=base_grad_batch_short.suffix_tensor,
        #     logits=base_grad_batch_short.logits
        # )
        def cut(x, batch_dim=False):
            if batch_dim:
                return torch.cat(
                    (
                        x[:, : prefix.shape[0]],
                        x[:, prefix.shape[0] + suffix.shape[0] :],
                    ),
                    dim=1,
                )

            return torch.cat(
                (x[: prefix.shape[0]], x[prefix.shape[0] + suffix.shape[0]]), dim=0
            )

        base_grad_batch_short.logits = cut(base_grad_batch_short.logits, batch_dim=True)
        base_grad_batch_long.logits = cut(base_grad_batch_long.logits, batch_dim=True)

        logits = base_grad_batch_short.logits
        start = base_grad_batch_short.logits.shape[1] - target.shape[0]
        end = base_grad_batch_short.logits.shape[1]  # does logits have batch?
        rewards = []
        # base_grad_batch_short.suffix_tensor = base_grad_batch_short.suffix_tensor
        reward_grad_batch = self.embedding_model.splice_embedded_batch(
            prefixes=[prefix],
            suffix_tokens=torch.zeros(0, device="cuda", dtype=torch.long),
            post_suffix_tokens=post_suffix,
            targets=[target],
            get_logits=False,
            # hot_suffix=base_grad_batch_short.suffix_tensor.detach(),
        )
        for target_logit_index in range(start, end - 1):
            # base_grad_batch_short.logits =
            # base_grad_batch_short.target_mask = torch.zeros(
            #     1,
            #     base_grad_batch_short.embeddings.shape[1] + 1,
            #     device=base_grad_batch_short.logits.device,
            #     dtype=torch.bool,
            # )
            # base_grad_batch_short.target_mask[:, -1] = 1

            ###
            target_logits = logits[:, target_logit_index - 1 : target_logit_index]
            new_embed = reward_grad_batch.embeddings[:target_logit_index]
            embedded_logits = self.embedding_model.embed(
                self.softmax(target_logits / temperature, dim=-1),
                onehot=True,
            ).squeeze(0)
            newer_embed = torch.cat([new_embed, embedded_logits], dim=1)

            reward = self(
                input_ids=None,
                attention_mask=torch.ones(newer_embed.shape[:2], device="cuda"),
                inputs_embeds=newer_embed,
            )
            ###

            rewards.append(reward.end_rewards)
            post_suffix = torch.cat([post_suffix, target[: start - target_logit_index]])
            # base_grad_batch_short.embeddings = torch.cat(
            #     [
            #         base_grad_batch_short.embeddings,
            #         base_grad_batch_long.embeddings[
            #             :, target_logit_index  # uncertain abt batch
            #         ].unsqueeze(1),
            #     ],
            #     dim=1,
            # )
        del base_grad_batch_long
        return (
            base_grad_batch_short,
            reward_grad_batch,
            torch.cat(rewards, dim=1).unsqueeze(0),
        )


#####
def get_reward_generator(
    device="cuda",
    model_path="ethz-spylab/reward_model",
):
    """
    Loads a reward model in evaluation mode on the specified device.

    Parameters:
    - device (str, optional)
    - model_path (str, optional): the name of the reward model to load

    Returns:
    - reward_model: The loaded and initialized reward model ready for generating rewards.
    """
    from arena_capstone.scripts.run_with_llama import load_from_pt

    print("Loading reward model")
    reward_model = load_from_pt(RewardGenerator, model_path)
    return reward_model
