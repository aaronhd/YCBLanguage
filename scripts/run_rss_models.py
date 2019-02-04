#!/usr/bin/env python3
__author__ = 'thomason-jesse'
# Takes in the split and labeled data folds, runs baselines and reports performance.

import argparse
import json
import os
import pandas as pd
from PIL import Image
from models import *
from sklearn.naive_bayes import GaussianNB
from torchvision.models import resnet
from torchvision.transforms import ToTensor
from torchvision.transforms import Normalize
from torchvision.transforms.functional import resize
from tqdm import tqdm
from utils import *


def keep_all_but(l1, l2, keep_but):
    return [l1[idx] for idx in range(len(l1)) if l2[idx] != keep_but]


def main(args, dv):

    # Near universals it would be better to read from data but which we're hard-coding.
    preps = ["in", "on"]
    modality_hidden_dim = 100  # fixed dimension to reduce RGBD, language, and vision representations to.

    # labels to use.
    train_label = args.train_objective + "_label"
    test_label = args.test_objective + "_label"
    models = args.models.split(',')

    # Read in torch-ready input data.
    print("Reading in torch-ready lists from json and converting them to tensors...")
    tr_outputs = {}
    tr_inputs_l = {}
    tr_inputs_v = {}
    tr_inputs_rgb = {}
    tr_inputs_d = {}
    te_outputs = {}
    te_inputs_l = {}
    te_inputs_v = {}
    te_inputs_rgb = {}
    te_inputs_d = {}
    for p in preps:
        fn = args.input + '.' + p
        with open(fn, 'r') as f:
            d = json.load(f)

            # # Can round "maybe" (1, of 0,1,2) class down to "no" at training time if we're testing on RGBD data
            # in general, if using five trial voting for Y/N instead of MC with M as a training example.
            # |
            # Need to round "maybe" (1, of 0,1,2) class down to "no" at training time in mturk labels if test objective
            # is two-class Y/N (1/0) only 'human' labels.
            if (('rgbd' in models and args.rgbd_m_as_disagreement) or
                    (args.test_objective == "human" and args.train_objective == "mturk")):
                cmtr = d["train"][train_label].count([1])
                d["train"][train_label] = [[2] if v[0] > 1 else [0] for v in d["train"][train_label]]
                print("... for %s training data, rounded %d Maybe labels down to No values" %
                      (p, cmtr))

            tr_outputs[p] = torch.tensor(keep_all_but(d["train"][train_label], d["train"][train_label], [-1]),
                                         dtype=torch.float).to(dv)
            tr_inputs_l[p] = torch.tensor(keep_all_but(d["train"]["lang"], d["train"][train_label], [-1]),
                                          dtype=torch.float).to(dv)
            tr_inputs_v[p] = torch.tensor(keep_all_but(d["train"]["vis"], d["train"][train_label], [-1]),
                                          dtype=torch.float).to(dv)
            tr_inputs_rgb[p] = torch.tensor(keep_all_but(d["train"]["rgb"], d["train"][train_label], [-1]),
                                            dtype=torch.float).to(dv) if d["train"]["rgb"] is not None else None
            tr_inputs_d[p] = torch.tensor(keep_all_but(d["train"]["d"], d["train"][train_label], [-1]),
                                          dtype=torch.float).to(dv) if d["train"]["d"] is not None else None

            te_outputs[p] = torch.tensor(keep_all_but(d["test"][test_label], d["test"][test_label], [-1]),
                                         dtype=torch.float).to(dv)
            te_inputs_l[p] = torch.tensor(keep_all_but(d["test"]["lang"], d["test"][test_label], [-1]),
                                          dtype=torch.float).to(dv)
            te_inputs_v[p] = torch.tensor(keep_all_but(d["test"]["vis"], d["test"][test_label], [-1]),
                                          dtype=torch.float).to(dv)
            te_inputs_rgb[p] = torch.tensor(keep_all_but(d["test"]["rgb"], d["test"][test_label], [-1]),
                                            dtype=torch.float).to(dv) if d["test"]["rgb"] is not None else None
            te_inputs_d[p] = torch.tensor(keep_all_but(d["test"]["d"], d["test"][test_label], [-1]),
                                          dtype=torch.float).to(dv) if d["test"]["d"] is not None else None
        print("... %s done; num train out %d, num test out %d" % (p, tr_outputs[p].shape[0], te_outputs[p].shape[0]))

        train_classes = set([int(v.item()) for v in tr_outputs[p]])
        test_classes = set([int(v.item()) for v in te_outputs[p]])
        if (train_classes != test_classes):
            print("...... WARNING: train classes " + str(train_classes) + " do not match test classes "
                  + str(test_classes) + " for " + p + "; will use union")
        classes = list(train_classes.union(test_classes))

    print("... classes: " + str(classes))
    bs = []
    rs = []

    # Majority class baseline.
    if 'mc' in models:
        print("Running majority class baseline...")
        bs.append("Majority Class")
        rs.append({})
        for p in preps:
            rs[-1][p] = run_majority_class([int(c[0]) for c in tr_outputs[p].detach().data.cpu().numpy().tolist()],
                                           [int(c[0]) for c in te_outputs[p].detach().data.cpu().numpy().tolist()],
                                           len(classes))
        print("... done")

    # Read in hyperparameters for feed forward networks or set defaults.
    ff_epochs = 10 if args.ff_epochs is None else args.ff_epochs
    if args.hyperparam_infile is not None:
        with open(args.hyperparam_infile, 'r') as f:
            d = json.load(f)
            ff_dropout = d['dropout']
            ff_lr = d['lr']
            ff_opt = d['opt']
            print("Loaded ff hyperparams: " + str(d))
    else:
        ff_dropout = 0
        ff_lr = 0.001
        ff_opt = 'sgd'
    ff_random_restarts = None if args.ff_random_restarts is None else \
        [int(s) for s in args.ff_random_restarts.split(',')]

    if 'rgbd' in models:
        print("Running RGBD models")
        bs.append("RGBD")
        rs.append({})
        for p in preps:
            if tr_inputs_rgb[p] is None:
                print("... ERROR: data had no RGBD features for prep %s" % p)
                del bs[-1]
                del rs[-1]
                continue

            # Couple the RGB and D inputs to feed into the paired inputs of the conv network.
            tr_inputs = [[tr_inputs_rgb[p][idx], tr_inputs_d[p][idx]] for idx in range(len(tr_outputs[p]))]
            te_inputs = [[te_inputs_rgb[p][idx], te_inputs_d[p][idx]] for idx in range(len(te_outputs[p]))]
            batch_size = 8  # TODO: hyperparam to set

            # Instantiate convolutional RGB and Depth models, then tie them together with a conv FF model.
            rgb_conv_model = ConvToLinearModel(dv, 3, modality_hidden_dim).to(dv)
            depth_conv_model = ConvToLinearModel(dv, 1, modality_hidden_dim).to(dv)
            model = ConvFFModel(dv, [rgb_conv_model, depth_conv_model], modality_hidden_dim * 2, len(classes)).to(dv)

            # Instantiate loss and optimizer.
            # TODO: optimizer is a hyperparam to set.
            loss_function = nn.CrossEntropyLoss()
            if ff_opt == 'sgd':
                optimizer = optim.SGD(model.param_list, lr=ff_lr)
            elif ff_opt == 'adagrad':
                optimizer = optim.Adagrad(model.param_list, lr=ff_lr)
            elif ff_opt == 'adam':
                optimizer = optim.Adam(model.param_list, lr=ff_lr)
            elif ff_opt == 'rmsprop':
                optimizer = optim.RMSprop(model.param_list, lr=ff_lr)
            else:
                raise ValueError('Unrecognized opt specification "' + ff_opt + '".')

            # Train model.
            if ff_random_restarts is None:
                seeds = [None]
            else:
                seeds = ff_random_restarts
                rs[-1][p] = []
            for seed in tqdm(seeds):
                if seed is not None:
                    # print("... %s with seed %d ..." % (p, seed))
                    np.random.seed(seed)
                    torch.manual_seed(seed)

                # Run training for specified number of epochs.
                best_acc = best_cm = tr_acc_at_best = trcm_at_best = None
                idxs = list(range(len(tr_inputs)))
                np.random.shuffle(idxs)
                tr_inputs = [tr_inputs[idx] for idx in idxs]
                tro = tr_outputs[p][idxs, :]
                num_trials = tr_inputs_rgb[p][0].shape[0]  # examples per input
                result = None
                for epoch in range(ff_epochs):
                    tloss = 0
                    trcm = np.zeros(shape=(len(classes), len(classes)))
                    tidx = 0
                    batches_run = 0
                    while tidx < len(tr_inputs):
                        model.zero_grad()
                        batch_in = [torch.zeros((batch_size * num_trials, tr_inputs_rgb[p][0].shape[1],
                                                 tr_inputs_rgb[p][0].shape[2], tr_inputs_rgb[p][0].shape[3])).to(dv),
                                    torch.zeros((batch_size * num_trials, tr_inputs_d[p][0].shape[1],
                                                 tr_inputs_d[p][0].shape[2], tr_inputs_d[p][0].shape[3])).to(dv)]
                        batch_gold = torch.zeros(batch_size * num_trials).to(dv)
                        for bidx in range(batch_size):
                            batch_in[0][bidx:bidx+num_trials, :, :, :] = tr_inputs_rgb[p][tidx]
                            batch_in[1][bidx:bidx+num_trials, :] = tr_inputs_d[p][tidx]
                            batch_gold[bidx:bidx+num_trials] = np.repeat(tro[tidx][0], num_trials)

                            tidx += 1
                            if tidx == len(tr_inputs):
                                break

                        logits = model(batch_in)
                        loss = loss_function(logits, batch_gold.long())
                        tloss += loss.data.item()
                        batches_run += 1

                        loss.backward()
                        optimizer.step()

                        # Calculated per instance, not per pair (e.g., x5 examples, individual voting).
                        for jdx in range(logits.shape[0]):
                            trcm[int(batch_gold[jdx])][int(logits[jdx].argmax(0))] += 1

                    tloss /= batches_run
                    tr_acc = get_acc(trcm)

                    with torch.no_grad():
                        cm = np.zeros(shape=(len(classes), len(classes)))
                        for jdx in range(len(te_inputs)):
                            trials_logits = model(te_inputs[jdx])  # will be full batch size wide
                            v = np.zeros(len(classes))
                            for tdx in range(num_trials):  # take a vote over trials (not whole logit size)
                                v[int(logits[tdx].argmax(0))] += 1
                            
                            if args.rgbd_m_as_disagreement:
                                if v[1] == v[2] == 0:  # all votes are for class negative
                                    cm[int(te_outputs[p][jdx])][0] += 1
                                elif v[0] == v[1] == 0:  # all votes are for class positive
                                    cm[int(te_outputs[p][jdx])][2] += 1
                                else:  # votes are split among different classes, so conservatively vote maybe
                                    cm[int(te_outputs[p][jdx])][1] += 1
                            else:
                                # If N/M/Y are all available, just use the majority vote across the trials
                                # to decide the predicted label of the pair.
                                cm[int(te_outputs[p][jdx])][int(v.argmax(0))] += 1

                        acc = get_acc(cm)
                        if best_acc is None or acc > best_acc:
                            best_acc = acc
                            best_cm = cm
                            tr_acc_at_best = tr_acc
                            trcm_at_best = trcm

                    result = best_acc, best_cm, tr_acc_at_best, trcm_at_best

                if seed is None:
                    rs[-1][p] = result
                else:
                    rs[-1][p].append(result)
        print("... done")

    if 'glove' in models:
        print("Running GLoVe models")
        bs.append("GLoVe FF")
        rs.append({})
        for p in preps:
            if ff_random_restarts is None:
                rs[-1][p] = run_ff_model(dv, tr_inputs_l[p], tr_outputs[p], te_inputs_l[p], te_outputs[p],
                                         tr_inputs_l[p].shape[1], modality_hidden_dim, len(classes),
                                         epochs=ff_epochs, dropout=ff_dropout, learning_rate=ff_lr, opt=ff_opt,
                                         verbose=args.verbose)
            else:
                rs[-1][p] = []
                for seed in ff_random_restarts:
                    print("... with seed " + str(seed) + "...")
                    np.random.seed(seed)
                    torch.manual_seed(seed)
                    rs[-1][p] = run_ff_model(dv, tr_inputs_l[p], tr_outputs[p], te_inputs_l[p], te_outputs[p],
                                             tr_inputs_l[p].shape[1], modality_hidden_dim, len(classes),
                                             epochs=ff_epochs, dropout=ff_dropout, learning_rate=ff_lr, opt=ff_opt,
                                             verbose=args.verbose)
        print("... done")

    if 'resnet' in models:
        print("Running ResNet FF models")
        bs.append("ResNet FF")
        rs.append({})
        for p in preps:
            if ff_random_restarts is None:
                rs[-1][p] = run_ff_model(dv, tr_inputs_v[p], tr_outputs[p], te_inputs_v[p], te_outputs[p],
                                         tr_inputs_v[p].shape[1], modality_hidden_dim, len(classes),
                                         epochs=ff_epochs, dropout=ff_dropout, learning_rate=ff_lr, opt=ff_opt,
                                         verbose=args.verbose)
            else:
                rs[-1][p] = []
                for seed in ff_random_restarts:
                    print("... with seed " + str(seed) + "...")
                    np.random.seed(seed)
                    torch.manual_seed(seed)
                    rs[-1][p] = run_ff_model(dv, tr_inputs_v[p], tr_outputs[p], te_inputs_v[p], te_outputs[p],
                                             tr_inputs_v[p].shape[1], modality_hidden_dim, len(classes),
                                             epochs=ff_epochs, dropout=ff_dropout, learning_rate=ff_lr, opt=ff_opt,
                                             verbose=args.verbose)
        print("... done")

    if ff_random_restarts is None:
        # Show results.
        print("Results:")
        for idx in range(len(bs)):
            print(" " + bs[idx] + ":")
            for p in preps:
                print("  " + p + ":\tacc %0.3f" % rs[idx][p][0] + "\t(train: %0.3f" % rs[idx][p][2] + ")")
                print("  \tf1  %0.3f" % get_f1(rs[idx][p][1]) + "\t(train: %0.3f" % get_f1(rs[idx][p][3]) + ")")
                print('\t(TeCM\t' + '\n\t\t'.join(['\t'.join([str(int(ct)) for ct in rs[idx][p][1][i]])
                                                 for i in range(len(rs[idx][p][1]))]) + ")")
                print('\t(TrCM\t' + '\n\t\t'.join(['\t'.join([str(int(ct)) for ct in rs[idx][p][3][i]])
                                                 for i in range(len(rs[idx][p][3]))]) + ")")

        # Write val accuracy results.
        if args.perf_outfile is not None:
            print("Writing results to '" + args.perf_outfile + "'...")
            with open(args.perf_outfile, 'w') as f:
                json.dump([{p: [rs[idx][p][0], get_f1(rs[idx][p][1])] for p in preps} for idx in range(len(rs))], f)
            print("... done")

    else:
        print("Results:")
        for idx in range(len(bs)):
            print(" " + bs[idx] + ":")
            for p in preps:
                avg_acc = np.average([rs[idx][p][jdx][0] for jdx in range(len(ff_random_restarts))])
                std_acc = np.std([rs[idx][p][jdx][0] for jdx in range(len(ff_random_restarts))])
                avg_tr_acc = np.average([rs[idx][p][jdx][2] for jdx in range(len(ff_random_restarts))])
                std_tr_acc = np.std([rs[idx][p][jdx][2] for jdx in range(len(ff_random_restarts))])
                avg_f1 = np.average([get_f1(rs[idx][p][jdx][1]) for jdx in range(len(ff_random_restarts))])
                std_f1 = np.std([get_f1(rs[idx][p][jdx][1]) for jdx in range(len(ff_random_restarts))])
                avg_tr_f1 = np.average([get_f1(rs[idx][p][jdx][3]) for jdx in range(len(ff_random_restarts))])
                std_tr_f1 = np.std([get_f1(rs[idx][p][jdx][3]) for jdx in range(len(ff_random_restarts))])
                print("  " + p + ":\tacc %0.3f+/-%0.3f" % (avg_acc, std_acc) +
                      "\t(train: %0.3f+/-%0.3f" % (avg_tr_acc, std_tr_acc) + ")")
                print("  \tf1  %0.3f+/-%0.3f" % (avg_f1, std_f1) +
                      "\t(train: %0.3f+/-%0.3f" % (avg_tr_f1, std_tr_f1) + ")")

        # Write out results for all seeds so that a downstream script can process them for stat sig.
        if args.perf_outfile is not None:
            print("Writing results to '" + args.perf_outfile + "'...")
            with open(args.perf_outfile, 'w') as f:
                json.dump([{p: {"acc": [rs[idx][p][jdx][0] for jdx in range(len(ff_random_restarts))],
                                "tr_acc": [rs[idx][p][jdx][2] for jdx in range(len(ff_random_restarts))],
                                "f1": [get_f1(rs[idx][p][jdx][1]) for jdx in range(len(ff_random_restarts))],
                                "tr_f1": [get_f1(rs[idx][p][jdx][3]) for jdx in range(len(ff_random_restarts))]}
                            for p in preps} for idx in range(len(rs))], f)
            print("... done")


if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    parser.add_argument('--input', type=str, required=True,
                        help="torch ready train/test input root to load as json")
    parser.add_argument('--models', type=str, required=True,
                        help="models to run (mc, glove, resnet, rgbd)")
    parser.add_argument('--train_objective', type=str, required=True,
                        help="either 'mturk', 'robo', or 'human'")
    parser.add_argument('--test_objective', type=str, required=True,
                        help="either 'mturk' or 'robo'")
    parser.add_argument('--rgbd_m_as_disagreement', type=int, required=False,
                        help=("if true, treat the M label as an inference-time-only classification that happens" +
                              " when votes are split between Y/N on the trials available for a pair; at training time," +
                              " the M labels are rounded down to N."))
    parser.add_argument('--verbose', type=int, required=False, default=0,
                        help="verbosity level")
    parser.add_argument('--hyperparam_infile', type=str, required=False,
                        help="input json for model hyperparameters")
    parser.add_argument('--perf_outfile', type=str, required=False,
                        help="output json for model performance")
    parser.add_argument('--ff_epochs', type=int, required=False,
                        help="override default number of epochs")
    parser.add_argument('--ff_random_restarts', type=str, required=False,
                        help="comma-separated list of random seeds to use; presents avg + stddev data instead of cms")
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    main(parser.parse_args(), device)
