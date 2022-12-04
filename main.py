import argparse
from tqdm import tqdm
import os
import numpy as np
import pandas as pd
from nltk.tokenize import word_tokenize

import torch
import torch.nn as nn
from torch.utils.data import DataLoader

from src.model import ArgumentModel
from src.utils import longestCommonSubsequence, get_optim, is_q
from src.dataset import ArgumentMiningDataset, ArgumentMiningTestDataset
torch.manual_seed(4096)
np.random.seed(4096)


def train(args):
    model = ArgumentModel(args.model).to(args.device)
    
    if args.optim2 is not None:
        optimizer, scheduler = get_optim(model.encoder.parameters(), args.optim, args.scheduler)
        # optimizer2, scheduler2 = get_optim(list(model.span_tagging.parameters()) + list(model.proj.parameters()), args.optim2, args.scheduler2)
        optimizer2, scheduler2 = get_optim(model.span_tagging.parameters(), args.optim2, args.scheduler2)
    else:
        optimizer, scheduler = get_optim(model.parameters(), args.optim, args.scheduler)
        optimizer2, scheduler2 = None, None
    
    trainset = ArgumentMiningDataset(path=args.data_path, split='train', batch_size=args.batch_size, model=args.model)
    devset = ArgumentMiningDataset(path=args.data_path, split='valid', batch_size=args.batch_size, model=args.model)
    trainloader = DataLoader(trainset, batch_size=1, shuffle=True, collate_fn=trainset.collate_fn, num_workers=0)
    devloader = DataLoader(devset, batch_size=1, shuffle=False, collate_fn=devset.collate_fn, num_workers=0)

    steps = 0
    update_time = 0
    best_score = 0

    span_loss = []

    for e in range(args.epoch):
        for _, A, S, _ in tqdm(trainloader):    
            A = {k: v.to(args.device) for k, v in A.items()}
            
            Outputs = model(A, S)
            loss = Outputs['loss'] / args.grad_steps
            loss.backward()
            del loss

            # log span loss
            span_loss.append(Outputs['loss'].item())

            steps += 1
            if steps % args.grad_steps == 0:
                update_time += 1

                torch.nn.utils.clip_grad_norm_(model.parameters(), 1)
                
                optimizer.step()
                optimizer.zero_grad()

                if scheduler:
                    scheduler.step()

                if optimizer2 is not None:
                    optimizer2.step()
                    optimizer2.zero_grad()
                    if scheduler2 is not None:
                        scheduler2.step()
                
                if update_time % args.log_step == 0:
                    print('[steps {0}] Training loss: {1:.4f}'.format(update_time, sum(span_loss) / len(span_loss)))
                    loss = []

        model.eval()
        print("Validating...", end='\r')
        valid_score = valid(model, devloader, devset.tokenizer, f'{args.result_path}/{args.exp_name}')
        if valid_score > best_score:
            torch.save(model.state_dict(), f'{args.result_path}/{args.exp_name}/best.ckpt')
            print(f"Better validation scores, save model to {args.result_path}/{args.exp_name}/best.ckpt")
            best_score = valid_score
        model.train()
    print(f"Best scores: {best_score}")

def valid(model, devloader, tokenizer, output):
    df_hyp = {'id': [], 'q': [], 'r': []}
    df_ref = {'id': [], 'q': [], 'r': []}
    with torch.no_grad():
        q_acc = []
        r_acc = []
        for ID, A, S, mapping in devloader:
            A = {k: v.to(args.device) for k, v in A.items()}

            Outputs = model(A)
            q = is_q(A['input_ids'], mapping).to(args.device)

            for i in range(A['input_ids'].size(0)):
                Q_s, Q_e, R_s, R_e = S[i].split([1, 1, 1, 1], -1)
                Q_s, Q_e, R_s, R_e = Q_s.squeeze(-1), Q_e.squeeze(-1), R_s.squeeze(-1), R_e.squeeze(-1)
            
                q_hyp = tokenizer.decode(A['input_ids'][i][torch.logical_and(Outputs['q_span'][i], q[i])], skip_special_tokens=True)
                r_hyp = tokenizer.decode(A['input_ids'][i][torch.logical_and(Outputs['r_span'][i], torch.logical_not(q[i]))], skip_special_tokens=True)
                q_hyp = ' '.join(word_tokenize(q_hyp))
                r_hyp = ' '.join(word_tokenize(r_hyp))

                df_hyp['id'].append(ID[i])
                df_hyp['q'].append(q_hyp)
                df_hyp['r'].append(r_hyp)

                q_max_acc = 0
                r_max_acc = 0
                ground_truth = torch.zeros(A['input_ids'][i].size())
                for j in range(Q_s.size(0)):
                    ground_truth[Q_s[j]: Q_e[j] + 1] = 1
                    ground_truth[R_s[j]: R_e[j] + 1] = 2
                q_ground_truth = (ground_truth == 1)
                r_ground_truth = (ground_truth == 2)

                q_tgt = tokenizer.decode(A['input_ids'][i][q_ground_truth], skip_special_tokens=True)
                r_tgt = tokenizer.decode(A['input_ids'][i][r_ground_truth], skip_special_tokens=True)
                q_tgt = ' '.join(word_tokenize(q_tgt))
                r_tgt = ' '.join(word_tokenize(r_tgt))

                df_ref['id'].append(ID[i])
                df_ref['q'].append(q_tgt)
                df_ref['r'].append(r_tgt)

                q_com = longestCommonSubsequence(q_hyp.split(), q_tgt.split())
                r_com = longestCommonSubsequence(r_hyp.split(), r_tgt.split())
                if len(q_tgt.split()) > 0 and len(r_tgt.split()) > 0:
                    q_hyp_acc = q_com / (len(q_hyp.split()) + len(q_tgt.split()) - q_com)
                    r_hyp_acc = r_com / (len(r_hyp.split()) + len(r_tgt.split()) - r_com)
                
                q_acc.append(q_hyp_acc)
                r_acc.append(r_hyp_acc)
    pd.DataFrame(df_hyp).to_csv(f'{output}/dev_hyp.csv', sep='|', index=False)
    pd.DataFrame(df_ref).to_csv(f'{output}/dev_ref.csv', sep='|', index=False)

    q_acc = sum(q_acc) / len(q_acc)
    r_acc = sum(r_acc) / len(r_acc)
    mean_acc = (q_acc + r_acc) / 2
    print("[valid] q_acc: {0:.4f}, r_acc: {1:.4f}, mean_acc: {2:.4f}".format(q_acc, r_acc, mean_acc))
    
    return mean_acc

def inference(args):
    model = ArgumentModel(args.model).to(args.device)
    model.load_state_dict(torch.load(args.ckpt))

    testset = ArgumentMiningTestDataset(path=args.data_path, batch_size=16, model=args.model)
    testloader = DataLoader(testset, batch_size=1, shuffle=False, collate_fn=testset.collate_fn, num_workers=0)
    
    df_hyp = {'id': [], 'q': [], 'r': []}
    with torch.no_grad():
        for ID, A, mapping in tqdm(testloader):
            A = {k: v.to(args.device) for k, v in A.items()}
            Outputs = model(A)
            q = is_q(A['input_ids'], mapping).to(args.device)
            
            for i in range(A['input_ids'].size(0)):
                q_hyp = testset.tokenizer.decode(A['input_ids'][i][torch.Logical_and(Outputs['q_span'][i], q[i])], skip_special_tokens=True)
                r_hyp = testset.tokenizer.decode(A['input_ids'][i][torch.Logical_and(Outputs['r_span'][i], torch.Logical_not(q[i]))], skip_special_tokens=True)
                q_hyp = ' '.join(word_tokenize(q_hyp))
                r_hyp = ' '.join(word_tokenize(r_hyp))
                
                df_hyp['id'].append(ID[i])
                df_hyp['q'].append(f"\"{q_hyp}\"")
                df_hyp['r'].append(f"\"{r_hyp}\"")
    pd.DataFrame(df_hyp).to_csv(f'{args.result_path}/{args.exp_name}/test_hyp.csv', sep=',', index=False)
    print('Done!')

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("--exp_name", '-n', type=str, default='', help="Name of experiment")
    parser.add_argument("--mode", '-m', type=str, required=True, help="Train or inference")
    parser.add_argument("--model", type=str, default="bert-base-uncased", help="Pretrained model name")
    parser.add_argument("--device", '-d', type=str, default="cuda:0", help="Training on which device")
    parser.add_argument("--epoch", '-e', type=int, default=10, help="Numbers of epoch")
    parser.add_argument("--batch_size", '-bs', type=int, default=1, help="Batch size")
    parser.add_argument("--grad_steps", '-gs', type=int, default=32, help="Gradient accumulation steps, 1080 sucks TAT")
    parser.add_argument("--optim", type=str, default='AdamW,0.00001,', help="optimizer config: \"type,lr,momentum\" ex: AdamW,0.0001,,")
    parser.add_argument("--optim2", type=str, default='SGD,0.01,0.9', help="optimizer for downstream model, if None, use the same optimizer with upstream")
    parser.add_argument("--scheduler", type=str, default='cosine_warmup,500,3000,3', help="scheduler config: \"type,warmup,allsteps,num_cycles\" ex: cosine_warmup,1000,2000,2")
    parser.add_argument("--scheduler2", type=str, default='cosine_warmup,200,3000,3', help="scheduler for downstream model, if None, not used during optimization")
    parser.add_argument("--log_step", type=int, default=100, help="log steps")
    parser.add_argument("--data_path", type=str, default="./data/", help="Path to a data folder containing [train.csv, valid.csv, test.csv]")
    parser.add_argument("--result_path", type=str, default="./result", help="Path to store ckpt and validation results.")
    parser.add_argument("--ckpt", type=str, default="", help="Path to a model ckpt for initialize the model")
    args = parser.parse_args()
    
    if not os.path.exists(args.result_path):
        os.makedirs(args.result_path)
    if not os.path.exists(f'{args.result_path}/{args.exp_name}'):
        os.makedirs(f'{args.result_path}/{args.exp_name}')

    if args.mode == 'train':
        train(args)
    elif args.mode == 'inference':
        assert os.path.exists(args.ckpt), "ckpt path not exists"
        inference(args)
    else:
        raise NotImplementedError("Not supported mode, choose from train or inference.")