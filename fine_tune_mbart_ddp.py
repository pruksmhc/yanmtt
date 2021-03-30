from transformers import AutoTokenizer
import time

import transformers

from transformers import MBartForConditionalGeneration, MBartConfig, get_linear_schedule_with_warmup
from transformers import AdamW


import os

import argparse

os.environ["CUDA_DEVICE_ORDER"]="PCI_BUS_ID"   # see issue #152
#os.environ["CUDA_VISIBLE_DEVICES"]="0,1,2,3,4,5,6,7"

import torch
import torch.nn as nn
from torch.nn.parallel import DistributedDataParallel
import torch.multiprocessing as mp
import sys
import torch.distributed as dist
from torch.optim import Adam

import math
import random
import numpy as np
import sacrebleu
from rouge_score import rouge_scorer


import gc


torch.manual_seed(621311)

def lmap(f, x):
    """list(map(f, x))"""
    return list(map(f, x))

def label_smoothed_nll_loss(lprobs, target, epsilon, ignore_index=0):
    """From fairseq. This returns the label smoothed loss."""
    if target.dim() == lprobs.dim() - 1:
        target = target.unsqueeze(-1)
    nll_loss = -lprobs.gather(dim=-1, index=target)
    smooth_loss = -lprobs.sum(dim=-1, keepdim=True)
    if ignore_index is not None:
        pad_mask = target.eq(ignore_index)
        nll_loss.masked_fill_(pad_mask, 0.0)
        smooth_loss.masked_fill_(pad_mask, 0.0)
    else:
        nll_loss = nll_loss.squeeze(-1)
        smooth_loss = smooth_loss.squeeze(-1)

    nll_loss = nll_loss.mean()
    smooth_loss = smooth_loss.mean()
    eps_i = epsilon / lprobs.size(-1)
    loss = (1.0 - epsilon) * nll_loss + eps_i * smooth_loss
    return loss


def shard_files(files, world_size):
    print("Sharding files into", world_size, "parts")
    for pair in files:
        infile = list(zip(open(files[pair][0]).readlines(), open(files[pair][1]).readlines()))
        num_lines = len(infile)
        lines_per_shard = math.ceil(num_lines/world_size)
        print("For language pair:",pair," the total number of lines are:", num_lines, "and number of lines per shard are:", lines_per_shard)
        for shard_id in range(world_size):
            srcoutfile = open(files[pair][0]+"."+"%02d" % shard_id, "w")
            tgtoutfile = open(files[pair][1]+"."+"%02d" % shard_id, "w")
            for src_line, tgt_line in infile[shard_id*lines_per_shard:(shard_id+1)*lines_per_shard]:
                srcoutfile.write(src_line)
                tgtoutfile.write(tgt_line)
            srcoutfile.flush()
            srcoutfile.close()
            tgtoutfile.flush()
            tgtoutfile.close()
        print("File for language pair", pair, "has been sharded.")
        sys.stdout.flush()

        
def get_sacrebleu(refs, hyp):
    """Returns sacrebleu score."""
    bleu = sacrebleu.corpus_bleu(hyp, refs)
    return bleu.score

def assert_all_frozen(model):
    """Checks if frozen parameters are all linked to each other or not. Ensures no disjoint components of graphs."""
    model_grads: List[bool] = list(grad_status(model))
    n_require_grad = sum(lmap(int, model_grads))
    npars = len(model_grads)
    assert not any(model_grads), f"{n_require_grad/npars:.1%} of {npars} weights require grad"

def grad_status(model):
    """Checks whether the parameter needs gradient or not. Part of asserting that the correct parts of the model are frozen."""
    return (par.requires_grad for par in model.parameters())


def freeze_params(model):
    """Set requires_grad=False for each of model.parameters()"""
    for par in model.parameters():
        par.requires_grad = False

def freeze_embeds(model):
    """Freeze token embeddings and positional embeddings for bart, just token embeddings for t5."""
    try:
        freeze_params(model.model.shared)
        for d in [model.model.encoder, model.model.decoder]:
            freeze_params(d.embed_positions)
            freeze_params(d.embed_tokens)
    except AttributeError:
        freeze_params(model.shared)
        for d in [model.encoder, model.decoder]:
            freeze_params(d.embed_tokens)

def generate_batches_eval(tok, args, file, slang):
    """Generates the source sentences for the dev set."""
    src_file = file #open(file)
    curr_batch_count = 0
    encoder_input_batch = []
    max_src_sent_len = 0

    for src_line in src_file:
        start = time.time()
        src_sent = src_line.strip()
        lang = "<2"+slang+">"
        src_sent_split = src_sent.split(" ")
        sent_len = len(src_sent_split)
        if sent_len > args.max_src_length:
            src_sent_split=src_sent_split[:args.max_src_length]
            src_sent = " ".join(src_sent_split)
            sent_len = args.max_src_length
        iids = tok(src_sent + " </s> " + lang, add_special_tokens=False, return_tensors="pt").input_ids
        curr_src_sent_len = len(iids[0])
        #print("Sentence lengths before and after segmentation: ", sent_len, curr_src_sent_len)
        if curr_src_sent_len > max_src_sent_len:
            max_src_sent_len = curr_src_sent_len

        encoder_input_batch.append(src_sent + " </s> " + lang)
        curr_batch_count += 1
        if curr_batch_count == args.dev_batch_size:
            input_ids = tok(encoder_input_batch, add_special_tokens=False, return_tensors="pt", padding=True, max_length=max_src_sent_len).input_ids
            if len(input_ids[0]) > args.max_src_length:
                input_ids = input_ids[:,:args.max_src_length]
            input_masks = (input_ids != tok.pad_token_id).int()
            end = time.time()
            #print(input_ids.size(), input_masks.size())
            yield input_ids, input_masks
            curr_batch_count = 0
            encoder_input_batch = []
            max_src_sent_len = 0

    if len(encoder_input_batch) != 0:
        input_ids = tok(encoder_input_batch, add_special_tokens=False, return_tensors="pt", padding=True, max_length=max_src_sent_len).input_ids
        if len(input_ids[0]) > args.max_src_length:
            input_ids = input_ids[:,:args.max_src_length]
        input_masks = (input_ids != tok.pad_token_id).int()
        yield input_ids, input_masks


def yield_corpus_indefinitely(corpus, language):
    """This shuffles the corpus at the beginning of each epoch and returns sentences indefinitely."""
    epoch_counter = 0
    while True:
        print("Shuffling corpus:", language)
        random.shuffle(corpus)
        for src_line, tgt_line in corpus:
            yield src_line, tgt_line
        
        epoch_counter += 1
        print("Finished epoch", epoch_counter, "for language:", language)
    return None, None


def generate_batches(tok, args, files, rank, mp_val_or_range=0.3, lamb=3.5):
    """Generates the source, target and source attention masks for the training set."""
    batch_count = 0
    language_list = list(files.keys())
    print("Training for:", language_list)
    language_file_dict = {}
    probs = {}
    for l in language_list:
        src_file_content = open(files[l][0]+"."+"%02d" % rank).readlines() ## +"."+"%02d" % rank for when we do distributed training
        tgt_file_content = open(files[l][1]+"."+"%02d" % rank).readlines() ## +"."+"%02d" % rank for when we do distributed training
        probs[l] = len(src_file_content)
        file_content = list(zip(src_file_content, tgt_file_content))
        language_file_dict[l] = yield_corpus_indefinitely(file_content, l)
    print("Corpora stats:", probs)
    probs_temp = {lang: probs[lang]/sum(probs.values()) for lang in probs}
    probs = probs_temp
    probs_temp = {lang: probs[lang]**(1.0/args.data_sampling_temperature) for lang in probs}
    probs = probs_temp
    probs_temp = {lang: probs[lang]/sum(probs.values()) for lang in probs}
    probs = [probs_temp[lang] for lang in language_list] ## NARROW IT DOWN
    num_langs = len(language_list)
    language_indices = list(range(num_langs))
    while batch_count != args.num_batches:
        curr_batch_count = 0
        encoder_input_batch = []
        decoder_input_batch = []
        decoder_label_batch = []
        batch_count += 1
        max_src_sent_len = 0
        max_tgt_sent_len = 0
        start = time.time()
        #for src_sent, tgt_sent in corpus_gen:
        sents_in_batch = 0
        while True:
            language_idx = random.choices(language_indices, probs)[0]
            src_sent, tgt_sent = next(language_file_dict[language_list[language_idx]])
            src_sent = src_sent.strip()
            tgt_sent = tgt_sent.strip()
            slangtlang = language_list[language_idx].strip().split("-")
            slang = "<2"+slangtlang[0]+">"
            tlang = "<2"+slangtlang[1]+">"
            src_sent_split = src_sent.split(" ")
            tgt_sent_split = tgt_sent.split(" ")
            tgt_sent_len = len(tgt_sent_split)
            src_sent_len = len(src_sent_split)
            if src_sent_len <=1 or tgt_sent_len <=1:
                continue
            else:   # Initial truncation
                if src_sent_len >= args.max_src_length:
                    src_sent_split = src_sent_split[:args.max_src_length]
                    src_sent = " ".join(src_sent_split)
                    src_sent_len = args.max_src_length
                if tgt_sent_len >= args.max_tgt_length:
                    tgt_sent_split = tgt_sent_split[:args.max_tgt_length]
                    tgt_sent = " ".join(tgt_sent_split)
                    tgt_sent_len = args.max_tgt_length
                
            if (slang == tlang and not args.is_summarization) or args.source_masking_for_bilingual: ## Copying task should DEFINITELY use source masking unless we are doing summarization. In fact a single condition based on a flag should be sufficient but I am too lazy to make a change. Come fight me if you disagree.
                if args.source_masking_for_bilingual:
                    mask_percent = random.uniform(0.0, mp_val_or_range[0]) ## Do less masking
                else:
                    if type(mp_val_or_range) is float:
                        mask_percent = mp_val_or_range
                    else:
                        mask_percent = random.uniform(mp_val_or_range[0], mp_val_or_range[1])
                mask_count = 0
                max_mask_count = int(mask_percent*src_sent_len)
                spans_to_mask = list(np.random.poisson(lamb, 1000))
                curr_sent_len = src_sent_len
                while mask_count < max_mask_count:
                    try:
                        span_to_mask = spans_to_mask[0]
                        del spans_to_mask[0]
                        if span_to_mask > (max_mask_count-mask_count): ## Cant mask more than the allowable number of tokens.
                            continue
                        idx_to_mask = random.randint(0, (curr_sent_len-1)-(span_to_mask-1))
                        if "[MASK]" not in src_sent_split[idx_to_mask:idx_to_mask+span_to_mask]:
                            src_sent_split[idx_to_mask:idx_to_mask+span_to_mask] = ["[MASK]"]
                            mask_count += span_to_mask
                            curr_sent_len -= (span_to_mask-1)
                    except:
                        break ## If we cannot get a properly masked sentence despite all our efforts then we just give up and continue with what we have so far.
                src_sent = " ".join(src_sent_split)
            iids = tok(src_sent + " </s> " + slang, add_special_tokens=False, return_tensors="pt").input_ids
            curr_src_sent_len = len(iids[0])
            
            iids = tok(tlang + " " + tgt_sent, add_special_tokens=False, return_tensors="pt").input_ids
            curr_tgt_sent_len = len(iids[0])
#             if curr_src_sent_len <= 1 or curr_tgt_sent_len <= 1:
#                 continue ## Definitely needs to be eliminated

            if curr_src_sent_len > max_src_sent_len:
                max_src_sent_len = curr_src_sent_len
            
            if curr_tgt_sent_len > max_tgt_sent_len:
                max_tgt_sent_len = curr_tgt_sent_len
            
            encoder_input_batch.append(src_sent + " </s> " + slang)
            decoder_input_batch.append(tlang + " " + tgt_sent)
            decoder_label_batch.append(tgt_sent + " </s>")
            sents_in_batch += 1
            curr_batch_count = max(max_src_sent_len, max_tgt_sent_len)*sents_in_batch ## curr_batch_count += curr_tgt_sent_len -- Old logic which created batches with 10x more tokens mostly padding
            if curr_batch_count > args.batch_size:
                break
        input_ids = tok(encoder_input_batch, add_special_tokens=False, return_tensors="pt", padding=True, max_length=max_src_sent_len).input_ids
        if len(input_ids[0]) > args.max_src_length:
            input_ids = input_ids[:,:args.max_src_length]
        input_masks = (input_ids != tok.pad_token_id).int()
        decoder_input_ids = tok(decoder_input_batch, add_special_tokens=False, return_tensors="pt", padding=True, max_length=max_tgt_sent_len).input_ids
        if len(decoder_input_ids[0]) > args.max_tgt_length:
            decoder_input_ids = decoder_input_ids[:,:args.max_tgt_length]
        labels = tok(decoder_label_batch, add_special_tokens=False, return_tensors="pt", padding=True, max_length=max_tgt_sent_len).input_ids
        if len(labels[0]) > args.max_tgt_length:
            labels = labels[:,:args.max_tgt_length]
        #print(input_ids.size(), input_masks.size(), decoder_input_ids.size(), labels.size())
        end = time.time()
        yield input_ids, input_masks, decoder_input_ids, labels
   
def init_weights(module, in_features, out_features):
    """Method to initialize model weights. Not used for now but might be used in the future. Tries to mimic t2t initialization."""
    if isinstance(module, nn.Linear):
        init_std = (3.0/(in_features+out_features))**(0.5)
        module.weight.data.normal_(mean=0.0, std=init_std)
        if module.bias is not None:
            module.bias.data.zero_()
    elif isinstance(module, nn.Embedding):
        init_std = (3.0/(out_features))**(0.5)
        module.weight.data.normal_(mean=0.0, std=init_std)
        if module.padding_idx is not None:
            module.weight.data[module.padding_idx].zero_()

def compute_distillation_losses(child_mod_compute, parent_mod_compute, target, ignore_index, args):
    distillation_losses_to_compute = args.distillation_styles.split(",")
    #print(distillation_losses_to_compute)
    all_distillation_losses = []
    for distillation_loss_to_compute in distillation_losses_to_compute:
        if distillation_loss_to_compute == "cross_entropy":
            parent_logits = parent_mod_compute.logits
            parent_lprobs = torch.nn.functional.log_softmax(parent_logits/args.softmax_temperature, dim=-1)
            child_logits = child_mod_compute.logits
            child_lprobs = torch.nn.functional.log_softmax(child_logits/args.softmax_temperature, dim=-1)
            if target.dim() == child_lprobs.dim() - 1:
                target = target.unsqueeze(-1)
    
            parent_softmax = torch.exp(parent_lprobs)
            #parent_softmax = parent_softmax.detach()
            pad_mask = target.eq(ignore_index)
            #print(parent_softmax.size(), child_lprobs.size())
            distillation_cross_entropy = parent_softmax*child_lprobs
            distillation_cross_entropy.masked_fill_(pad_mask, 0.0)
            #print(distillation_cross_entropy.size())
            distillation_cross_entropy = distillation_cross_entropy.sum(dim=-1)
            #print(distillation_cross_entropy.size())
            distillation_cross_entropy = distillation_cross_entropy.mean() * args.softmax_temperature**2
            #print(distillation_cross_entropy.size())
            all_distillation_losses.append(distillation_cross_entropy)
            #print(all_distillation_losses)
    return -torch.mean(torch.stack(all_distillation_losses), dim=0)

def model_create_load_run_save(gpu, args, train_files, dev_files):
    """The main function which does the magic. Should be split into multiple parts in the future."""
    rank = args.nr * args.gpus + gpu
    print("Launching process:", rank)
    if not args.single_gpu:
        dist.init_process_group(backend='nccl', init_method='env://', world_size=args.world_size, rank=rank)
    
    if args.shard_files and rank == 0:
        shard_files(train_files, args.world_size)
    
    dist.barrier()
    
    tok = AutoTokenizer.from_pretrained(args.tokenizer_name_or_path, do_lower_case=False, use_fast=False, keep_accents=True)
#     rouge = PerlRouge(rouge_n_max=3, rouge_l=True, rouge_w=True,
#     rouge_w_weight=1.2, rouge_s=True, rouge_su=True, skip_gap=4)
    scorer = rouge_scorer.RougeScorer(['rouge1', 'rougeL'], use_stemmer=False)

    #files = {"as": "data/as/as.txt", "bn": "data/bn/bn.txt", "en": "data/en/en.txt", "gu": "data/gu/gu.txt", "hi": "data/hi/hi.txt", "kn": "data/kn/kn.txt", "ml": "data/ml/ml.txt", "mr": "data/mr/mr.txt", "or": "data/or/or.txt", "pa": "data/pa/pa.txt", "ta": "data/ta/ta.txt", "te": "data/te/te.txt"}  ## Get this from command line
    
#     if args.mnmt:
#         special_tokens_dict = {'additional_special_tokens': ["<s>", "</s>"] + ["<2"+lang+">" for lang in files.keys()] + ["<2"+args.slang+">", "<2"+args.tlang+">"]}
#     else:
#         special_tokens_dict = {'additional_special_tokens': ["<s>", "</s>"] + ["<2"+lang+">" for lang in files.keys()] + ["<2"+sl+">" for sl in (args.slang).strip().split(",")] + ["<2"+tl+">" for tl in (args.tlang).strip().split(" ")]}
    

#     num_added_toks = tok.add_special_tokens(special_tokens_dict)

    print("Tokenizer is:", tok)
    
    if args.single_gpu:
        print(f"Running checkpoint example on rank {rank}.")
    else:
        print(f"Running DDP checkpoint example on rank {rank}.")
    if args.fp16:
        print("We will do fp16 training")
        scaler = torch.cuda.amp.GradScaler()
    else:
        print("We will do fp32 training")
    
    if args.encoder_tying_config is not None:
        print("We will use recurrently stacked layers for the encoder with configuration:", args.encoder_tying_config)
    if args.decoder_tying_config is not None:
        print("We will use recurrently stacked layers for the decoder with configuration:", args.decoder_tying_config)
        
    config = MBartConfig(vocab_size=len(tok), encoder_layers=args.encoder_layers, decoder_layers=args.decoder_layers, dropout=args.dropout, attention_dropout=args.attention_dropout, activation_dropout=args.activation_dropout, encoder_attention_heads=args.encoder_attention_heads, decoder_attention_heads=args.decoder_attention_heads, encoder_ffn_dim=args.encoder_ffn_dim, decoder_ffn_dim=args.decoder_ffn_dim, d_model=args.d_model, add_final_layer_norm=args.add_final_layer_norm, normalize_before=args.normalize_before, normalize_embedding=args.normalize_embedding, scale_embedding=args.scale_embedding, pad_token_id=tok.pad_token_id, eos_token_id=tok(["</s>"]).input_ids[0][1], bos_token_id=tok(["<s>"]).input_ids[0][1], static_position_embeddings=True, encoder_tying_config=args.encoder_tying_config, decoder_tying_config=args.decoder_tying_config)
    model = MBartForConditionalGeneration(config)
    model.train()
    
    if args.distillation:
        print("We will do distillation from a parent model.")
        parent_config = MBartConfig(vocab_size=len(tok), encoder_layers=args.parent_encoder_layers, decoder_layers=args.parent_decoder_layers, dropout=args.parent_dropout, attention_dropout=args.parent_attention_dropout, activation_dropout=args.parent_activation_dropout, encoder_attention_heads=args.parent_encoder_attention_heads, decoder_attention_heads=args.parent_decoder_attention_heads, encoder_ffn_dim=args.parent_encoder_ffn_dim, decoder_ffn_dim=args.parent_decoder_ffn_dim, d_model=args.parent_d_model, add_final_layer_norm=args.add_final_layer_norm, normalize_before=args.normalize_before, normalize_embedding=args.normalize_embedding, scale_embedding=args.scale_embedding, pad_token_id=tok.pad_token_id, eos_token_id=tok(["</s>"]).input_ids[0][1], bos_token_id=tok(["<s>"]).input_ids[0][1], static_position_embeddings=True, encoder_tying_config=args.encoder_tying_config, decoder_tying_config=args.decoder_tying_config)
        parent_model = MBartForConditionalGeneration(config)
        parent_model.cuda(gpu)
        parent_model.train() ## We do this to enable dropout but we wont have an optimizer for this so we wont train this model. For now. Future implementations should ask if we want to do co-distill or not. By co-distillation I mean, the parent will learn together with the child.
        if args.single_gpu:
            pass
        else:
            parent_model = DistributedDataParallel(parent_model, device_ids=[gpu], output_device=gpu)
#         for param in parent_model.parameters():
#             param.requires_grad = False
        print("Loading a parent model from which distillation will be done.")
        if args.single_gpu:
            pass
        else:
            dist.barrier()
        # configure map_location properly
        map_location = {'cuda:%d' % 0: 'cuda:%d' % rank}
        parent_checkpoint_dict = torch.load(args.parent_pretrained_model, map_location=map_location)
        if type(parent_checkpoint_dict) == dict:
            parent_model.load_state_dict(parent_checkpoint_dict['model'])
        else:
            parent_model.load_state_dict(parent_checkpoint_dict)

    torch.cuda.set_device(gpu)
    
    if args.freeze_embeddings:
        print("Freezing embeddings")
        freeze_embeds(model)
    if args.freeze_encoder:
        print("Freezing encoder")
        freeze_params(model.get_encoder())
        assert_all_frozen(model.get_encoder())

    model.cuda(gpu)

    no_decay = ["bias", "LayerNorm.weight"]
    optimizer_grouped_parameters = [
        {
            "params": [p for n, p in model.named_parameters() if not any(nd in n for nd in no_decay)],
            "weight_decay": args.weight_decay,
        },
        {
            "params": [p for n, p in model.named_parameters() if any(nd in n for nd in no_decay)],
            "weight_decay": 0.0,
        },
    ]
    
    optimizer = AdamW(optimizer_grouped_parameters, lr=args.lr, eps=1e-09)
    
    if args.single_gpu:
        pass
    else:
        model = DistributedDataParallel(model, device_ids=[gpu], output_device=gpu)
    scheduler = get_linear_schedule_with_warmup(optimizer, args.warmup_steps, args.num_batches*args.world_size)
    
    while scheduler.get_lr()[0] < 1e-7:
        scheduler.step()
    print("Initial LR is:", scheduler.get_lr()[0])
    
    if args.pretrained_bilingual_model == "" and args.pretrained_model != "":
        print("Loading a pretrained mbart model")
        if args.single_gpu:
            pass
        else:
            dist.barrier()
        # configure map_location properly
        map_location = {'cuda:%d' % 0: 'cuda:%d' % rank}
        checkpoint_dict = torch.load(args.pretrained_model, map_location=map_location)
        if type(checkpoint_dict) == dict:
            model.load_state_dict(checkpoint_dict['model'])
        else:
            model.load_state_dict(checkpoint_dict)
    elif args.pretrained_bilingual_model != "":
        print("Loading a previous checkpoint")
        if args.single_gpu:
            pass
        else:
            dist.barrier()
            # configure map_location properly
        map_location = {'cuda:%d' % 0: 'cuda:%d' % rank}
        checkpoint_dict = torch.load(CHECKPOINT_PATH, map_location=map_location)
        if type(checkpoint_dict) == dict:
            model.load_state_dict(checkpoint_dict['model'])
            optimizer.load_state_dict(checkpoint_dict['optimizer'])
            scheduler.load_state_dict(checkpoint_dict['scheduler'])
            ctr = checkpoint_dict['ctr']
        else:
            model.load_state_dict(checkpoint_dict)
            ctr = 0
    else:
        print("Training from scratch")
        ctr = 0
        
    print("Using label smoothing of", args.label_smoothing)
    print("Using gradient clipping norm of", args.max_gradient_clip_value)
    print("Using softmax temperature of", args.softmax_temperature)
    if args.max_ent_weight != -1:
        print("Doing entropy maximization during loss computation.")
    #config.save_pretrained(args.fine_tuned_model+"/config")
    ctr = 0
    global_sbleu_history = []
    max_global_sbleu = 0
    max_global_sbleu_step = 0
    individual_sbleu_history = {dev_pair: [] for dev_pair in dev_files}
    max_individual_sbleu = {dev_pair: 0 for dev_pair in dev_files}
    max_individual_sbleu_step = {dev_pair: 0 for dev_pair in dev_files}
    curr_eval_step = 0
    annealing_attempt = 0
    inps = {dev_pair: [inpline.strip() for inpline in open(dev_files[dev_pair][0])] for dev_pair in dev_files}
    if args.is_summarization:
        refs = {dev_pair: [[refline.strip() for refline in open(dev_files[dev_pair][1])]] for dev_pair in dev_files}
        scores = {dev_pair: 0 for dev_pair in dev_files}
    else:
        refs = {dev_pair: [[refline.strip() for refline in open(dev_files[dev_pair][1])]] for dev_pair in dev_files}
    for input_ids, input_masks, decoder_input_ids, labels in generate_batches(tok, args, train_files, rank, (0.30, 0.40), 3.5):
        optimizer.zero_grad()
        start = time.time()
        if ctr % args.eval_every == 0:
            CHECKPOINT_PATH = args.fine_tuned_model
            if rank == 0:
                if not args.no_eval:
                    print("Running eval on dev set(s)")
                    hyp = {dev_pair: [] for dev_pair in dev_files}
                    sbleus = {}
                    if args.single_gpu:
                        model.eval()
                    else:
                        model.module.eval()
                    checkpoint_dict = {'model': model.state_dict(), 'optimizer': optimizer.state_dict(), 'scheduler': scheduler.state_dict(), 'ctr': ctr}
                    for dev_pair in dev_files:
                        slangtlang =dev_pair.strip().split("-")
                        slang=slangtlang[0]
                        tlang=slangtlang[1]
                        for dev_input_ids, dev_input_masks in generate_batches_eval(tok, args, inps[dev_pair], slang): #infinite_same_sentence(10000):
                            start = time.time()
                            if args.single_gpu:
                                dev_input_ids=dev_input_ids.to(gpu)
                                with torch.no_grad():
                                    translations = model.generate(dev_input_ids, use_cache=True, num_beams=1, max_length=int(len(input_ids[0])*args.max_decode_length_multiplier), min_length=int(len(input_ids[0])*args.min_decode_length_multiplier), early_stopping=True, attention_mask=dev_input_masks.to(gpu), pad_token_id=tok.pad_token_id, eos_token_id=tok(["</s>"]).input_ids[0][1], decoder_start_token_id=tok(["<2"+tlang+">"]).input_ids[0][1], bos_token_id=tok(["<s>"]).input_ids[0][1], length_penalty=args.length_penalty, repetition_penalty=args.repetition_penalty, encoder_no_repeat_ngram_size=args.encoder_no_repeat_ngram_size, no_repeat_ngram_size=args.no_repeat_ngram_size)
                            else:
                                with torch.no_grad():
                                    translations = model.module.generate(dev_input_ids.to(gpu), use_cache=True, num_beams=1, max_length=int(len(input_ids[0])*args.max_decode_length_multiplier), min_length=int(len(input_ids[0])*args.min_decode_length_multiplier), early_stopping=True, attention_mask=dev_input_masks.to(gpu), pad_token_id=tok.pad_token_id, eos_token_id=tok(["</s>"]).input_ids[0][1], decoder_start_token_id=tok(["<2"+tlang+">"]).input_ids[0][1], bos_token_id=tok(["<s>"]).input_ids[0][1], length_penalty=args.length_penalty, repetition_penalty=args.repetition_penalty, encoder_no_repeat_ngram_size=args.encoder_no_repeat_ngram_size, no_repeat_ngram_size=args.no_repeat_ngram_size)
                            dev_input_ids=dev_input_ids.to('cpu')
                            translations=translations.to('cpu')
                            for translation in translations:
                                translation  = tok.decode(translation, skip_special_tokens=True, clean_up_tokenization_spaces=False) 
                                hyp[dev_pair].append(translation)
                        if args.is_summarization:
                            for curr_ref, curr_pred in zip(refs[dev_pair][0], hyp[dev_pair]):
                                score = scorer.score(curr_ref, curr_pred)
                                scores[dev_pair] += score['rougeL'].fmeasure
                            sbleu = scores[dev_pair]/len(hyp[dev_pair])
                            metric = 'Rouge'
                        else:
                            sbleu = get_sacrebleu(refs[dev_pair], hyp[dev_pair])
                            metric = 'BLEU'
                        individual_sbleu_history[dev_pair].append([sbleu, ctr])
                        sbleus[dev_pair] = sbleu
                        print(metric, "score using sacrebleu after", ctr, "iterations is", sbleu, "for language pair", dev_pair)
                        if sbleu > max_individual_sbleu[dev_pair]:
                            max_individual_sbleu[dev_pair] = sbleu
                            max_individual_sbleu_step[dev_pair] = curr_eval_step
                            print("New peak reached for", dev_pair,". Saving.")
                            torch.save(checkpoint_dict, CHECKPOINT_PATH+".best_dev_bleu."+dev_pair+"."+str(ctr))
                            if args.single_gpu:
                                torch.save(model.state_dict(), CHECKPOINT_PATH+".best_dev_bleu."+dev_pair+"."+str(ctr)+".pure_model") ## Pure model with ddp markers and no optimizer info.
                            else:
                                torch.save(model.module.state_dict(), CHECKPOINT_PATH+".best_dev_bleu."+dev_pair+"."+str(ctr)+".pure_model") ## Pure model without any ddp markers or optimizer info.

                    ## Global stats
                    sbleu = sum(sbleus.values())/len(sbleus)
                    global_sbleu_history.append([sbleu, ctr])
                    print("Global", metric, "score using sacrebleu after", ctr, "iterations is:", sbleu)
                    if sbleu > max_global_sbleu:
                        max_global_sbleu = sbleu
                        max_global_sbleu_step = curr_eval_step
                        print("New peak reached. Saving.")
                        torch.save(checkpoint_dict, CHECKPOINT_PATH+".best_dev_bleu.global."+str(ctr))
                        if args.single_gpu:
                            torch.save(model.state_dict(), CHECKPOINT_PATH+".best_dev_bleu.global."+str(ctr)+".pure_model") ## Pure model with ddp markers and no optimizer info.
                        else:
                            torch.save(model.module.state_dict(), CHECKPOINT_PATH+".best_dev_bleu.global."+str(ctr)+".pure_model") ## Pure model without any ddp markers or optimizer info.
                    if curr_eval_step - max_global_sbleu_step > (args.early_stop_checkpoints + annealing_attempt*args.additional_early_stop_checkpoints_per_anneal_step):
                        if annealing_attempt < args.max_annealing_attempts:
                            annealing_attempt += 1
                            curr_lr = scheduler.get_lr()[0]
                            print("LR before annealing is:", curr_lr)
                            while scheduler.get_lr()[0] > (curr_lr/args.learning_rate_scaling):
                                scheduler.step()
                            print("LR after annealing is:", scheduler.get_lr()[0])

                        else:
                            print("We have seemingly converged as", metric, "failed to increase for the following number of checkpoints:", args.early_stop_checkpoints+annealing_attempt*args.additional_early_stop_checkpoints_per_anneal_step, ". You may want to consider increasing the number of tolerance steps, doing additional annealing or having a lower peak learning rate or something else.")
                            print("Terminating training")
                            print("Global dev", metric, "history:", global_sbleu_history)
                            print("Individual", metric, "history:", individual_sbleu_history )
                            break
                    curr_eval_step += 1

                    if args.single_gpu:
                        model.train()
                    else:
                        model.module.train()

                else:
                    if ctr % 10000 == 0:
                        print("No evaluation based early stopping so saving every 10,000 checkpoints.")
                        checkpoint_dict = {'model': model.state_dict(), 'optimizer': optimizer.state_dict(), 'scheduler': scheduler.state_dict(), 'ctr': ctr}
                        torch.save(checkpoint_dict, CHECKPOINT_PATH+"."+str(ctr))
                print("Saving the model")
                sys.stdout.flush()
                # All processes should see same parameters as they all start from same
                # random parameters and gradients are synchronized in backward passes.
                # Therefore, saving it in one process is sufficient.
                checkpoint_dict = {'model': model.state_dict(), 'optimizer': optimizer.state_dict(), 'scheduler': scheduler.state_dict(), 'ctr': ctr}
                torch.save(checkpoint_dict, CHECKPOINT_PATH)
                if args.single_gpu:
                    torch.save(model.module.state_dict(), CHECKPOINT_PATH+".pure_model")
                else:
                    torch.save(model.state_dict(), CHECKPOINT_PATH+".pure_model")
                

            # Use a barrier() to make sure that process 1 loads the model after process
            # 0 saves it.
            if args.single_gpu:
                pass
            else:
                dist.barrier()
            # configure map_location properly
            print("Loading from checkpoint")
            sys.stdout.flush()
            map_location = {'cuda:%d' % 0: 'cuda:%d' % rank}
            checkpoint_dict = torch.load(CHECKPOINT_PATH, map_location=map_location)
            model.load_state_dict(checkpoint_dict['model'])
            optimizer.load_state_dict(checkpoint_dict['optimizer']) ## Dubious
            scheduler.load_state_dict(checkpoint_dict['scheduler']) ## Dubious
            
        input_ids=input_ids.to(gpu)
        input_masks=input_masks.to(gpu)
        decoder_input_ids=decoder_input_ids.to(gpu)
        labels=labels.to(gpu)
        try:
            if args.fp16:
                with torch.cuda.amp.autocast():
                    mod_compute = model(input_ids=input_ids, attention_mask=input_masks ,decoder_input_ids=decoder_input_ids, output_hidden_states=args.distillation, output_attentions=args.distillation)
                    logits = mod_compute.logits
                    lprobs = torch.nn.functional.log_softmax(logits/args.softmax_temperature, dim=-1)
                    loss = label_smoothed_nll_loss(
                        lprobs, labels, args.label_smoothing, ignore_index=tok.pad_token_id
                    )
                    loss = loss*args.softmax_temperature
                    if args.max_ent_weight != -1:
                        assert (args.max_ent_weight >= 0 and args.max_ent_weight <= 1)
                        lprobs = torch.nn.functional.log_softmax(logits, dim=-1) ## No tempering here
                        entropy = -(torch.exp(lprobs)*lprobs).mean()
                        loss = loss*(1-args.max_ent_weight) - entropy*args.max_ent_weight ## Maximize the entropy so a minus is needed.
                    if args.distillation:
                        with torch.no_grad():
                            parent_mod_compute = parent_model(input_ids=input_ids, attention_mask=input_masks ,decoder_input_ids=decoder_input_ids, output_hidden_states=args.distillation, output_attentions=args.distillation)
                        distillation_loss = compute_distillation_losses(mod_compute, parent_mod_compute, labels, tok.pad_token_id, args)
                        loss = args.distillation_loss_weight*distillation_loss + (1.0 - distillation_loss_weight)*loss
            else:
                mod_compute = model(input_ids=input_ids, attention_mask=input_masks, decoder_input_ids=decoder_input_ids, output_hidden_states=args.distillation, output_attentions=args.distillation)
                logits = mod_compute.logits
                lprobs = torch.nn.functional.log_softmax(logits/args.softmax_temperature, dim=-1)
                loss = label_smoothed_nll_loss(
                    lprobs, labels, args.label_smoothing, ignore_index=tok.pad_token_id
                )
                loss = loss*args.softmax_temperature
                if args.max_ent_weight != -1:
                    assert (args.max_ent_weight >= 0 and args.max_ent_weight <= 1)
                    lprobs = torch.nn.functional.log_softmax(logits, dim=-1) ## No tempering here
                    entropy = -(torch.exp(lprobs)*lprobs).mean()
                    loss = loss*(1-args.max_ent_weight) - entropy*args.max_ent_weight ## Maximize the entropy so a minus is needed.
                if args.distillation:
                    with torch.no_grad():
                        parent_mod_compute = parent_model(input_ids=input_ids, attention_mask=input_masks ,decoder_input_ids=decoder_input_ids, output_hidden_states=args.distillation, output_attentions=args.distillation)
                        distillation_loss = compute_distillation_losses(mod_compute, parent_mod_compute, labels, tok.pad_token_id, args)
                        loss = args.distillation_loss_weight*distillation_loss + (1.0 - args.distillation_loss_weight)*loss

                    
        except Exception as e:
            print("NAN loss was computed or something messed up")
            print(e)
            sys.stdout.flush()
        input_ids=input_ids.to('cpu')
        input_masks=input_masks.to('cpu')
        decoder_input_ids=decoder_input_ids.to('cpu')
        labels=labels.to('cpu')
        if args.fp16:
            scaler.scale(loss).backward()
        else:
            pass
        if args.fp16:
            if args.max_gradient_clip_value != 0.0:
                scaler.unscale_(optimizer)
                torch.nn.utils.clip_grad_norm_(model.parameters(), args.max_gradient_clip_value)
            scaler.step(optimizer)
            scaler.update()
        else:
            loss.backward()
            if args.max_gradient_clip_value != 0.0:
                torch.nn.utils.clip_grad_norm_(model.parameters(), args.max_gradient_clip_value)
            optimizer.step()
        scheduler.step()
        lv = loss.detach().cpu().numpy()
        if ctr % 10 == 0 and rank == 0:
            print(ctr, lv)
            sys.stdout.flush()
        end = time.time()
        ctr += 1
#         print("After iter:", ctr)
#         objs = []
#         for obj in gc.get_objects():
#             try:
#                 if torch.is_tensor(obj) or (hasattr(obj, 'data') and torch.is_tensor(obj.data)):
#                     objs.append((type(obj), obj.size()))
#             except:
#                 pass
#         print(len(objs))
    
    CHECKPOINT_PATH = args.fine_tuned_model
    print("Saving the model after the final step")
    # All processes should see same parameters as they all start from same
    # random parameters and gradients are synchronized in backward passes.
    # Therefore, saving it in one process is sufficient.
    print("The best bleu was:", max_global_sbleu)
    print("The corresponding step was:", max_global_sbleu_step*args.eval_every)
    checkpoint_dict = {'model': model.state_dict(), 'optimizer': optimizer.state_dict(), 'scheduler': scheduler.state_dict(), 'ctr': ctr}
    torch.save(checkpoint_dict, CHECKPOINT_PATH)
    if args.single_gpu:
        torch.save(model.state_dict(), CHECKPOINT_PATH+".pure_model") ## Pure model with ddp markers and no optimizer info
    else:
        torch.save(model.module.state_dict(), CHECKPOINT_PATH+".pure_model") ## Pure model without any ddp markers or optimizer info.
    if args.single_gpu:
        pass
    else:
        dist.destroy_process_group()
    

def run_demo():
    parser = argparse.ArgumentParser()
    parser.add_argument('-n', '--nodes', default=1,
                        type=int, metavar='N')
    parser.add_argument('-g', '--gpus', default=1, type=int,
                        help='number of gpus per node')
    parser.add_argument('-nr', '--nr', default=0, type=int,
                        help='ranking within the nodes')
    parser.add_argument('-a', '--ipaddr', default='localhost', type=str, 
                        help='IP address of the main node')
    parser.add_argument('-p', '--port', default='26023', type=str, 
                        help='Port main node')
    parser.add_argument('--freeze_embeddings', action='store_true', 
                        help='Should freeze embeddings during fine tuning?')
    parser.add_argument('--freeze_encoder', action='store_true', 
                        help='Should we freeze encoder during fine tuning?')
    parser.add_argument('--add_final_layer_norm', action='store_true', 
                        help='Should we add a final layer norm?')
    parser.add_argument('--normalize_before', action='store_true', 
                        help='Should we normalize before doing attention?')
    parser.add_argument('--normalize_embedding', action='store_true', 
                        help='Should we normalize embeddings?')
    parser.add_argument('--scale_embedding', action='store_true', 
                        help='Should we scale embeddings?')
    parser.add_argument('--mnmt', action='store_true', 
                        help='Are we training MNMT models? If so then the datagen will be slightly tweaked. We will also expect that training and development files will be comma separated when passed as arguments. The slang and tlang markers will also be comma separated and will follow the order of these files.')
    parser.add_argument('--encoder_layers', default=6, type=int, help="The value for number of encoder layers")
    parser.add_argument('--decoder_layers', default=6, type=int, help="The value for number of decoder layers")
    parser.add_argument('--label_smoothing', default=0.1, type=float, help="The value for label smoothing")
    parser.add_argument('--weight_decay', default=0.0001, type=float, help="The value for weight decay")
    parser.add_argument('--lr', default=7e-4, type=float, help="The value for the learning rate")
    parser.add_argument('--dropout', default=0.1, type=float, help="The value for embedding dropout")
    parser.add_argument('--attention_dropout', default=0.1, type=float, help="The value for attention dropout")
    parser.add_argument('--activation_dropout', default=0.1, type=float, help="The value for activation dropout")
    parser.add_argument('--data_sampling_temperature', default=5.0, type=float, help="The value for the data sampling temperature")
    parser.add_argument('--repetition_penalty', default=1.0, type=float, 
                        help='To prevent repetition during decoding. 1.0 means no repetition. 1.2 was supposed to be a good value for some settings according to some researchers.')
    parser.add_argument('--no_repeat_ngram_size', default=0, type=int, 
                        help='N-grams of this size will never be repeated in the decoder. Lets play with 2-grams as default.')
    parser.add_argument('--length_penalty', default=1.0, type=float, 
                        help='Set to more than 1.0 for longer sentences.')
    parser.add_argument('--encoder_no_repeat_ngram_size', default=0, type=int, 
                        help='N-gram sizes to be prevented from being copied over from encoder. Lets play with 2-grams as default.')
    parser.add_argument('--encoder_tying_config', default=None, type=str, 
                        help='What should be the parameter tying configuration? 1-1-1-1-1-1 means 6 layers where all are shared. 1-1-2-2-3-3 means 6 layers, 3 unique layers and each one is recurred twice before passing to another layer. 1-2-3-1-2-3 means 6 layers, 3 unique layers and recurrence is done twice after all layers have been passed through. The default None implies a 1-2-3-4-...-N setup')
    parser.add_argument('--decoder_tying_config', default=None, type=str,
                        help='What should be the parameter tying configuration? 1-1-1-1-1-1 means 6 layers where all are shared. 1-1-2-2-3-3 means 6 layers, 3 unique layers and each one is recurred twice before passing to another layer. 1-2-3-1-2-3 means 6 layers, 3 unique layers and recurrence is done twice after all layers have been passed through. The default None implies a 1-2-3-4-...-N setup')
    parser.add_argument('--softmax_temperature', default=1.0, type=float, help="The value for the softmax temperature")
    parser.add_argument('--encoder_attention_heads', default=8, type=int, help="The value for number of encoder attention heads")
    parser.add_argument('--decoder_attention_heads', default=8, type=int, help="The value for number of decoder attention heads")
    parser.add_argument('--decoder_ffn_dim', default=2048, type=int, help="The value for decoder ff hidden dim")
    parser.add_argument('--encoder_ffn_dim', default=2048, type=int, help="The value for encoder ff hidden dim")
    parser.add_argument('--d_model', default=512, type=int, help="The value for model hidden size")
    parser.add_argument('--eval_every', default=1000, type=int, help="The number of iterations after which an evaluation must be done. Also saves a checkpoint every these number of steps.")
    parser.add_argument('--max_gradient_clip_value', default=1.0, type=float, help="The max value for gradient norm value")

    parser.add_argument('--pretrained_model', default='', type=str, 
                        help='Path to the pretrained model')
    parser.add_argument('--pretrained_bilingual_model', default='', type=str, 
                        help='Path to the pretrained bilingual model. Use this if you want to continue training a bilingual model.')
    parser.add_argument('-m', '--fine_tuned_model', default='pytorch.bin', type=str, 
                        help='Path to save the fine tuned model')
    parser.add_argument('--warmup_steps', default=16000, type=int,
                        help='Scheduler warmup steps')
    parser.add_argument('--batch_size', default=1024, type=int, 
                        help='Train batch sizes in tokens')
    parser.add_argument('--dev_batch_size', default=1024, type=int, 
                        help='Dev batch sizes in lines')
    parser.add_argument('--max_src_length', default=256, type=int, 
                        help='Maximum token length for source language')
    parser.add_argument('--max_tgt_length', default=256, type=int, 
                        help='Maximum token length for target language')
    parser.add_argument('--early_stop_checkpoints', default=10, type=int, 
                        help='Number of checkpoints to wait to see if BLEU increases.')
    parser.add_argument('--learning_rate_scaling', default=2, type=int, 
                        help='How much should the LR be divided by during annealing?. Set num_batches to a larger value or else you will see lr go to zero too soon.')
    parser.add_argument('--max_annealing_attempts', default=2, type=int, 
                        help='Number of times LR should be annealed.')
    parser.add_argument('--additional_early_stop_checkpoints_per_anneal_step', default=5, type=int, 
                        help='How many additional checkpoints should we wait till declaring convergence? This will be multiplied with the annealing step number.')
    parser.add_argument('--num_batches', default=1000000, type=int, 
                        help='Number of batches to train on')
    parser.add_argument('--max_decode_length_multiplier', default=1.5, type=float, 
                        help='This multiplied by the source sentence length will be the maximum decoding length.')
    parser.add_argument('--min_decode_length_multiplier', default=0.25, type=float, 
                        help='This multiplied by the source sentence length will be the minimum decoding length.')
    parser.add_argument('--train_slang', default='en', type=str, 
                        help='Source language(s) for training')
    parser.add_argument('--tokenizer_name_or_path', default='ai4bharat/indic-bert', type=str, 
                        help='Name of or path to the pre-trained indic language tokenizer')
    parser.add_argument('--train_tlang', default='hi', type=str, 
                        help='Target language(s) for training')
    parser.add_argument('--train_src', default='', type=str, 
                        help='Source language training sentences')
    parser.add_argument('--train_tgt', default='', type=str, 
                        help='Target language training sentences')
    parser.add_argument('--dev_slang', default='en', type=str, 
                        help='Source language(s) for training')
    parser.add_argument('--dev_tlang', default='hi', type=str, 
                        help='Target language(s) for training')
    parser.add_argument('--dev_src', default='', type=str, 
                        help='Source language(s) development sentences')
    parser.add_argument('--dev_tgt', default='', type=str, 
                        help='Target language(s) development sentences')
    parser.add_argument('--fp16', action='store_true', 
                        help='Should we use fp16 training?')
    parser.add_argument('--no_eval', action='store_true', 
                        help='Should we skip evaluation?')
    parser.add_argument('--source_masking_for_bilingual', action='store_true', 
                        help='Should we use masking on source sentences when training on parallel corpora?')
    parser.add_argument('--is_summarization', action='store_true', 
                        help='Should we use masking on source sentences when training on parallel corpora?')
    parser.add_argument('--max_ent_weight', type=float, default=-1.0, 
                        help='Should we maximize softmax entropy? If the value is anything between 0 and 1 then yes. If its -1.0 then no maximization will be done.')
    parser.add_argument('--single_gpu', action='store_true', 
                        help='Should we use single gpu training?')
    parser.add_argument('--shard_files', action='store_true', 
                        help='Should we shard the training data? Set to true only if the data is not already pre-sharded.')
    ### Distillation flags
    parser.add_argument('--distillation', action='store_true', 
                        help='Should we perform distillation from a parent model? If so then you must specify the model using "parent_pretrained_model". There are several distillation options check the flag called "distillation_styles".')
    parser.add_argument('--parent_pretrained_model', default='', type=str, 
                        help='Path to the parent pretrained model for distillation. The pretrained_model flag will be used to initialize the child model.')
    parser.add_argument('--distillation_loss_weight', type=float, default=0.7, 
                        help='All the distillation losses will be averaged and then multiplied by this weight before adding it to the regular xentropy loss which will be weighted by (1- distillation_loss_weight).')
    parser.add_argument('--distillation_styles', default='cross_entropy', type=str, 
                        help='One or more of softmax_distillation, attention_distillation, hidden_layer_regression. For attention distillation you must make sure that the number of attention heads between the parent and child are the same and for hidden layer regression you must make sure that the hidden size (d_model) is the same for the parent and child. In both these cases, you should also specify the layer mapping. See the "distillation_layer_mapping" flag.')
    parser.add_argument('--distillation_layer_mapping', default='1-1,2-2,3-3,4-4,5-5,6-6', type=str, 
                        help='This indicates the mappings between the parent and child model. The same flag is used for the encoder and the decoder. If you want to map the 2nd parent layer to the first child layer then use 2-1. Note that the layers are not zero indexed as per the description. Ensure that your indices are correct because checking is not done at the moment. If you get weird results then first make sure that your flags are correctly set. If the parent has 6 layers and the child has 3 layers then something like 6-4 will definitely throw an error. User beware! Dokuro mark.')
    parser.add_argument('--parent_encoder_layers', default=6, type=int, help="The value for number of encoder layers")
    parser.add_argument('--parent_decoder_layers', default=6, type=int, help="The value for number of decoder layers")
    parser.add_argument('--parent_dropout', default=0.1, type=float, help="The value for embedding dropout")
    parser.add_argument('--parent_attention_dropout', default=0.1, type=float, help="The value for attention dropout")
    parser.add_argument('--parent_activation_dropout', default=0.1, type=float, help="The value for activation dropout")
    parser.add_argument('--parent_encoder_attention_heads', default=8, type=int, help="The value for number of encoder attention heads")
    parser.add_argument('--parent_decoder_attention_heads', default=8, type=int, help="The value for number of decoder attention heads")
    parser.add_argument('--parent_decoder_ffn_dim', default=2048, type=int, help="The value for decoder ff hidden dim")
    parser.add_argument('--parent_encoder_ffn_dim', default=2048, type=int, help="The value for encoder ff hidden dim")
    parser.add_argument('--parent_d_model', default=512, type=int, help="The value for model hidden size")
    ###
    args = parser.parse_args()
    print("IP address is", args.ipaddr)
    
    args.world_size = args.gpus * args.nodes                #
    
    train_files = {}
    if args.mnmt:
        slangs = args.train_slang.strip().split(",")
        tlangs = args.train_tlang.strip().split(",")
        train_srcs = args.train_src.strip().split(",")
        train_tgts = args.train_tgt.strip().split(",")
        train_files = {slang+"-"+tlang: (train_src, train_tgt) for slang, tlang, train_src, train_tgt in zip(slangs, tlangs, train_srcs, train_tgts)}
    else:
        train_files = {args.train_slang+"-"+args.train_tlang : (args.train_src, args.train_tgt)}
    print("Training files are:", train_files)
    
    dev_files = {}
    if args.mnmt:
        slangs = args.dev_slang.strip().split(",")
        tlangs = args.dev_tlang.strip().split(",")
        dev_srcs = args.dev_src.strip().split(",")
        dev_tgts = args.dev_tgt.strip().split(",")
        dev_files = {slang+"-"+tlang: (dev_src, dev_tgt) for slang, tlang, dev_src, dev_tgt in zip(slangs, tlangs, dev_srcs, dev_tgts)}
    else:
        dev_files = {args.dev_slang+"-"+args.dev_tlang : (args.dev_src, args.dev_tgt)}
    print("Development files are:", dev_files)
    
    os.environ['MASTER_ADDR'] = args.ipaddr              #
    os.environ['MASTER_PORT'] = args.port                      #
    if args.single_gpu:
        print("Non ddp model being trained")
        model_create_load_run_save(0, args,train_files, dev_files)#
    else:
        mp.spawn(model_create_load_run_save, nprocs=args.gpus, args=(args,train_files, dev_files))         #
    
if __name__ == "__main__":
    run_demo()
    
    
    
## Defunct code

    #print(model)
#     if args.pretrained_bilingual_model == "" and args.pretrained_model == "":
#         print("Manual initialization")
#         for module in model.modules():
#             if isinstance(module, nn.Linear):
#                 print("Initializing", module)
#                 init_weights(module, module.in_features, module.out_features)
#             if isinstance(module, torch.nn.Embedding):
# #                 print(type(module))
# #                 if isinstance(module, transformers.models.mbart.modeling_mbart.MBartLearnedPositionalEmbedding):
# #                     print("Not initializing", module)
# #                 else:
#                 print("Initializing", module)
#                 init_weights(module, len(tok), args.d_model) ## Might need modification
            