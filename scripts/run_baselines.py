#!/usr/bin/env python3
__author__ = 'thomason-jesse'
# Takes in the split and labeled data folds, runs simple baselines and reports performance.

import argparse
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import json
from functools import reduce
from torch.autograd import Variable


# Get accuracy from a confusion matrix.
def get_acc(cm):
    return np.trace(cm) / np.sum(cm)


# Count the majority class label in the train set and use it as a classification decision on every instance
# in the test set.
# tr_l - training labels
# te_l - testing labels
def run_majority_class(tr_l, te_l):

    # Train: count the majority class across all labels.
    cl = {}
    for l in tr_l:
        if l not in cl:
            cl[l] = 0
        cl[l] += 1
    mc = sorted(cl, key=cl.get, reverse=True)[0]

    # Test: for every instance, guess the majority class.
    cm = np.zeros(shape=(len(cl), len(cl)))
    for l in te_l:
        cm[l][mc] += 1

    # Return accuracy and cm.
    return get_acc(cm), cm


# A naive bayes implementation that assumes known categorical feature values.
# fs - feature shape, list of size |F| with entries the range of categorical values per feature.
# tr_f - training features
# tr_l - training labels
# te_f - testing features
# te_l - testing labels
def run_cat_naive_bayes(fs, tr_f, tr_l, te_f, te_l,
                        verbose=False, smooth=0):

    # Train: calculate the prior per class and the conditional likelihood of each feature given the class.
    if verbose:
        print("NB: training on " + str(len(tr_f)) + " inputs...")
    cf = {}  # class frequency
    ff_c = {}  # categorical feature frequency given class
    for idx in range(len(tr_f)):
        c = tr_l[idx]
        x = tr_f[idx]
        if c not in cf:
            cf[c] = smooth
            ff_c[c] = [{cat: smooth for cat in fs[jdx]} for jdx in range(len(x))]
        cf[c] += 1
        for jdx in range(len(x)):
            ff_c[c][jdx][x[jdx]] += 1
    cp = {c: cf[c] / float(len(tr_f)) for c in cf}  # class prior
    fp_c = {c: [{cat: ff_c[c][jdx][cat] / float(cf[c]) for cat in fs[jdx]} for jdx in range(len(tr_f[0]))]
            for c in ff_c}  # categorical feature probability given class
    if verbose:
        print("NB: ... done")

    # Test: calculate the joint likelihood of each class for each instance conditioned on its features.
    if verbose:
        print("NB: testing on " + str(len(te_f)) + " outputs...")
    cm = np.zeros(shape=(len(cp), len(cp)))
    for idx in range(len(te_f)):
        tc = te_l[idx]
        x = te_f[idx]
        y_probs = [np.log(cp[c]) + reduce(lambda _x, _y: _x + _y, [np.log(fp_c[c][jdx][x[jdx]]) for jdx in range(len(x))])
                   for c in cf]
        cm[tc][y_probs.index(max(y_probs))] += 1
    if verbose:
        print("NB: ... done")

    # Return accuracy and cm.
    return get_acc(cm), cm


# Based on:
# https://pytorch.org/tutorials/beginner/nlp/sequence_models_tutorial.html
class LSTMTagger(nn.Module):

    def __init__(self, width, vocab_size, num_classes):
        super(LSTMTagger, self).__init__()
        self.width = width
        self.vocab_size = vocab_size
        self.num_classes = num_classes

        self.word_embeddings = nn.Embedding(vocab_size, width)
        self.lstm_src = nn.LSTM(width, width)
        self.lstm_des = nn.LSTM(width, width)
        self.hidden2class = nn.Linear(width*2, num_classes)

        self.hidden_src = self.init_hidden()
        self.hidden_des = self.init_hidden()

    def init_hidden(self):
        return (torch.zeros(1, 1, self.width),
                torch.zeros(1, 1, self.width))

    def forward(self, res):
        embeds_src = self.word_embeddings(res[0])
        embeds_des = self.word_embeddings(res[0])
        lstm_src_out, self.hidden_src = self.lstm_src(embeds_src.view(len(res[0]), 1, -1), self.hidden_src)
        lstm_des_out, self.hidden_des = self.lstm_des(embeds_des.view(len(res[0]), 1, -1), self.hidden_des)
        hcat = torch.cat((lstm_src_out, lstm_des_out), 0)
        final_logits = self.hidden2class(hcat.view(len(res[0]), -1))[-1, :]
        final_logits = final_logits.view(1, len(final_logits))
        final_scores = F.softmax(final_logits, dim=1)
        return final_scores, final_logits


# Train an LSTM language encoder to predict the category label given language descriptions.
# tr_f - lists of descriptions for training instances
# tr_l - training labels
# te_f - lists of descriptions for testing instances
# te_l - testing labels
# verbose - whether to print epoch-wise progress
# epochs - number of epochs to train
# width - width of the encoder LSTM
# batch_size - number of training examples to run before gradient update
# epochs - number of epochs to run over data; if None, runs over all data once
def run_lang_2_label(maxlen, word_to_i,
                     tr_enc_exps, tr_l, te_enc_exps, te_l,
                     verbose=False, width=16, batch_size=100, epochs=None):

    # Train on the cross product of every description of source and description of destination object.
    if verbose:
        print("L2L: preparing training and testing data...")
    classes = []
    tr_inputs = []
    tr_outputs = []
    p = nn.ConstantPad1d((0, maxlen), word_to_i["<_>"])
    for idx in range(len(tr_enc_exps)):
        for ridx in range(len(tr_enc_exps[idx][0])):
            for rjdx in range(len(tr_enc_exps[idx][1])):
                tr_inputs.append([p(torch.tensor(tr_enc_exps[idx][0][ridx], dtype=torch.long)),
                                  p(torch.tensor(tr_enc_exps[idx][1][rjdx], dtype=torch.long))])
                tr_outputs.append(tr_l[idx])
                if tr_l[idx] not in classes:
                    classes.append(tr_l[idx])
    tr_outputs = torch.tensor(tr_outputs, dtype=torch.long).view(len(tr_outputs), 1)
    if epochs is None:  # iterate given the batch size to cover all data at least once
        epochs = int(np.ceil(len(tr_inputs) / float(batch_size)))

    # At test time, run the model on the cross product of descriptions for the pair and sum logits.
    te_inputs = []
    for idx in range(len(te_enc_exps)):
        pairs_in = []
        for ridx in range(len(te_enc_exps[idx][0])):
            for rjdx in range(len(te_enc_exps[idx][1])):
                pairs_in.append([p(torch.tensor(te_enc_exps[idx][0][ridx], dtype=torch.long)),
                                 p(torch.tensor(te_enc_exps[idx][1][rjdx], dtype=torch.long))])
        te_inputs.append(pairs_in)
    if verbose:
        print("L2L: ... done")

    # Train: train a neural model for a fixed number of epochs.
    if verbose:
        print("L2L: training on " + str(len(tr_inputs)) + " inputs with batch size " + str(batch_size) + " for " + str(epochs) + " epochs...")
    m = LSTMTagger(width, len(word_to_i), len(classes))
    loss_function = nn.CrossEntropyLoss(ignore_index = word_to_i['<_>'])
    optimizer = optim.SGD(m.parameters(), lr=0.001)  # TODO: this could be touched up.
    cm = acc = None
    idxs = list(range(len(tr_inputs)))
    np.random.shuffle(idxs)
    tr_inputs = [tr_inputs[idx] for idx in idxs]
    tr_outputs = tr_outputs[idxs, :]
    idx = 0
    c = 0
    for epoch in range(epochs):
        tloss = 0
        c = 0
        while c < batch_size:
            m.zero_grad()
            m.hidden_src = m.init_hidden()
            m.hidden_des = m.init_hidden()
            _, logits = m(tr_inputs[idx])
            loss = loss_function(logits, tr_outputs[idx])
            tloss += loss.data.item()
            loss.backward()
            optimizer.step()
            c += 1
            idx += 1
            if idx == len(tr_inputs):
                idx = 0
        tloss /= batch_size
        print("... epoch " + str(epoch) + " train loss " + str(tloss))
    if verbose:
        print("L2L: ... done")

    # Test: report accuracy after training.
    if verbose:
        print("L2L: calculating test-time accuracy...")
    with torch.no_grad():
        cm = np.zeros(shape=(len(classes), len(classes)))
        for jdx in range(len(te_inputs)):
            slogits = torch.zeros(1, len(classes))
            for vidx in range(len(te_inputs[jdx])):
                _, logits = m(te_inputs[jdx][vidx])
                slogits += logits
            pc = slogits.max(1)[1]
            cm[te_l[jdx]][pc] += 1
        acc = get_acc(cm)
    if verbose:
        print("L2L: ... done; " + str(acc))

    # Return accuracy and cm.
    return acc, cm

# Make the language structures needed for other models.
# tr_f - referring expression structure for training
# te_f - referring expresison structure for testing
# inc_test - whether to 'see' test words unseen during training (UNK when false)
def make_lang_structures(tr_f, te_f, inc_test=False):
    word_to_i = {"<?>": 0, "<s>": 1, "<e>": 2, "<_>": 4}
    i_to_word = {0: "<?>", 1: "<s>", 2: "<e>", 3: "<_>"}
    maxlen = 0
    tr_enc_exps = []
    for idx in range(len(tr_f)):
        pair_re_is = []
        for oidx in range(2):
            ob_re_is = []
            for re in tr_f[idx][oidx]:
                re_is = [word_to_i["<s>"]]
                for w in re:
                    if w not in word_to_i:
                        i = len(i_to_word)
                        word_to_i[w] = i
                        i_to_word[i] = w
                    re_is.append(word_to_i[w])
                re_is.append(word_to_i["<e>"])
                maxlen = max(maxlen, len(re_is))
                ob_re_is.append(re_is)
            pair_re_is.append(ob_re_is)
        tr_enc_exps.append(pair_re_is)

    te_enc_exps = []
    for idx in range(len(te_f)):
        pair_re_is = []
        for oidx in range(2):
            ob_re_is = []
            for re in te_f[idx][oidx]:
                re_is = [word_to_i["<s>"]]
                for w in re:
                    if w not in word_to_i:
                        if inc_test:
                            i = len(i_to_word)
                            word_to_i[w] = i
                            i_to_word[i] = w
                        else:
                            i = word_to_i["<?>"]
                    re_is.append(i)
                re_is.append(word_to_i["<e>"])
                maxlen = max(maxlen, len(re_is))
                ob_re_is.append(re_is)
            pair_re_is.append(ob_re_is)
        te_enc_exps.append(pair_re_is)

    return word_to_i, i_to_word, maxlen, tr_enc_exps, te_enc_exps


# Get GLoVe vectors from target input file for given vocabulary.
# fn - the filename where the GLoVe vectors live.
# ws - the list of words to look up.
def get_glove_vectors(fn, ws):
    g = {}
    with open(fn, 'r') as f:
        lines = f.readlines()
        for line in lines:
            p = line.strip().split(' ')
            w = p[0]
            if w in ws:
                v = np.array([float(n) for n in p[1:]])
                g[w] = v
    m = 0
    for w in ws:
        if w not in g:
            m += 1
            v = np.zeros(len(g[list(g.keys())[0]]))
            g[w] = v
    assert set(g.keys()) == ws
    return g, m


# Run a GLoVe-based model (either perceptron or FF network with relu activations)
# glove - a dictionary mapping all vocabulary words to their glove embedding vectors
# tr_f - lists of descriptions for training instances
# tr_l - training labels
# te_f - lists of descriptions for testing instances
# te_l - testing labels
# layers - a list of hidden widths for a FF network or None to create a linear perceptron
# verbose - whether to print epoch-wise progress
# Reference: https://github.com/jcjohnson/pytorch-examples/blob/master/nn/two_layer_net_nn.py
def run_glove_based_model(glove,
                          tr_f, tr_l, te_f, te_l,
                          layers=None, batch_size=None, epochs=None,
                          verbose=False):
    assert batch_size is not None or epochs is not None

    # Prepare the data.
    print("Glove: preparing training and testing data...")
    tr_inputs = []
    te_inputs = []
    classes = []
    for model_in, orig_in, orig_out in [[tr_inputs, tr_f, tr_l], [te_inputs, te_f, te_l]]:
        for idx in range(len(orig_in)):
            ob1_ws = [w for ws in orig_in[idx][0] for w in ws]
            avg_ob1_glove = np.sum([glove[w] for w in ob1_ws], axis=0) / len(ob1_ws)
            ob2_ws = [w for ws in orig_in[idx][1] for w in ws]
            avg_ob2_glove = np.sum([glove[w] for w in ob2_ws], axis=0) / len(ob2_ws)
            incat = np.concatenate((avg_ob1_glove, avg_ob2_glove))
            model_in.append(torch.tensor(incat, dtype=torch.float))
            inwidth = len(model_in[-1])
            if orig_out[idx] not in classes:
                classes.append(orig_out[idx])
    outwidth = len(classes)
    tr_outputs = torch.tensor(tr_l, dtype=torch.long).view(len(tr_l), 1)
    te_outputs = torch.tensor(te_l, dtype=torch.long).view(len(te_l), 1)
    if epochs is None:  # iterate given the batch size to cover all data at least once
        epochs = int(np.ceil(len(tr_inputs) / float(batch_size)))
    if batch_size is None:
        batch_size = len(tr_inputs)
    print("Glove: ... done")

    # Construct the model with specified number of hidden layers / dimensions (or none) and relu activations between.
    print("Glove: constructing model...")
    if layers is not None:
        l = [nn.Linear(inwidth, layers[0])]
        for idx in range(1, len(layers)):
            l.append(torch.nn.ReLU())
            l.append(nn.Linear(layers[idx - 1], layers[idx]))
        l.append(torch.nn.ReLU())
        l.append(nn.Linear(layers[-1], outwidth))
    else:
        l = [nn.Linear(inwidth, outwidth)]
    model = nn.Sequential(*l)
    print("Glove: ... done")

        # Train: train a neural model for a fixed number of epochs.
    if verbose:
        print("Glove: training on " + str(len(tr_inputs)) + " inputs with batch size " + str(batch_size) + " for " + str(epochs) + " epochs...")
    loss_function = nn.CrossEntropyLoss()
    optimizer = optim.SGD(model.parameters(), lr=0.001)  # TODO: this could be touched up.
    cm = acc = None
    idxs = list(range(len(tr_inputs)))
    np.random.shuffle(idxs)
    tr_inputs = [tr_inputs[idx] for idx in idxs]
    tr_outputs = tr_outputs[idxs, :]
    idx = 0
    c = 0
    for epoch in range(epochs):
        tloss = 0
        c = 0
        while c < batch_size:
            model.zero_grad()
            logits = model(tr_inputs[idx])
            loss = loss_function(logits.view(1, len(logits)), tr_outputs[idx])
            tloss += loss.data.item()
            loss.backward()
            optimizer.step()
            c += 1
            idx += 1
            if idx == len(tr_inputs):
                idx = 0
        tloss /= batch_size

        with torch.no_grad():
            cm = np.zeros(shape=(len(classes), len(classes)))
            for jdx in range(len(te_inputs)):
                logits = model(te_inputs[jdx])
                pc = logits.max(0)[1]
                cm[te_l[jdx]][pc] += 1
            acc = get_acc(cm)

        print("... epoch " + str(epoch) + " train loss " + str(tloss) + "; test accuracy " + str(acc))
    if verbose:
        print("Glove: ... done")

    return acc, cm


def main(args):
    assert args.baseline is None or args.baseline in ['majority', 'nb_names', 'nb_bow', 'lstm', 'glove']
    assert args.glove_infile is not None or (args.baseline is not None and args.baseline != 'glove')
    verbose = True if args.verbose == 1 else False

    # Read in labeled folds.
    print("Reading in labeled folds from '" + args.infile + "'...")
    with open(args.infile, 'r') as f:
        all_d = json.load(f)
        names = all_d["names"]
        lf = all_d["folds"]
        train = lf['train']
        test = lf['dev']  # only ever peak at the dev set.
        preps = train.keys()
    print("... done")

    # Read in metadata.
    print("Reading in metadata from '" + args.metadata_infile + "'...")
    with open(args.metadata_infile, 'r') as f:
        d = json.load(f)
        res = d["res"]
    print("... done")

    bs = []
    rs = []

    # Majority class baseline.
    if args.baseline is None or args.baseline == 'majority':
        print("Running majority class baseline...")
        bs.append("Majority Class")
        rs.append({})
        for p in preps:
            rs[-1][p] = run_majority_class(train[p]["label"], test[p]["label"])
        print("... done")

    # Majority class | object ids baseline.
    if args.baseline is None or args.baseline == 'nb_names':
        print("Running Naive Bayes baseline...")
        bs.append("Naive Bayes Obj One Hots")
        rs.append({})
        fs = [range(len(names)), range(len(names))]  # Two one-hot vectors of which object name was seen.
        for p in preps:
            tr_f = [[train[p][s][idx] for s in ["ob1", "ob2"]] for idx in
                    range(len(train[p]["ob1"]))]
            te_f = [[test[p][s][idx] for s in ["ob1", "ob2"]] for idx in
                    range(len(test[p]["ob1"]))]
            rs[-1][p] = run_cat_naive_bayes(fs, tr_f, train[p]["label"], te_f, test[p]["label"], verbose=verbose)
        print("... done")

    # Prep language dictionary.
    if args.baseline is None or args.baseline in ['nb_bow', 'lstm', 'glove']:
        print("Preparing infrastructure to run language models...")
        tr_f = {}
        te_f = {}
        word_to_i = {}
        word_to_i_all = {}
        i_to_word = {}
        maxlen = {}
        tr_enc_exps = {}
        te_enc_exps = {}
        for p in preps:
            tr_f = [[res[train[p][s][idx]] for s in ["ob1", "ob2"]] for idx in
                     range(len(train[p]["ob1"]))]
            te_f = [[res[test[p][s][idx]] for s in ["ob1", "ob2"]] for idx in
                     range(len(test[p]["ob1"]))]
            word_to_i[p], i_to_word[p], maxlen[p], tr_enc_exps[p], te_enc_exps[p] = make_lang_structures(tr_f, te_f)
            word_to_i_all[p], _, _, _, _ = make_lang_structures(tr_f, te_f, inc_test=True)
        print("... done")

    # Language naive bayes (bag of words)
    if args.baseline is None or args.baseline == 'nb_bow':
        print("Running BoW Naive Bayes...")
        bs.append("BoW Naive Bayes")
        rs.append({})
        for p in preps:
            print("... prepping model input and output data...")
            fs = [[0, 1] for _ in range(len(word_to_i[p]) * 2)]  # Two BoW vectors; one for each object.
            tr_inputs = []
            tr_outputs = []
            for idx in range(len(tr_enc_exps[p])):
                w_in_re_src = set()
                for ridx in range(len(tr_enc_exps[p][idx][0])):
                    w_in_re_src.update(tr_enc_exps[p][idx][0][ridx])
                w_in_re_tar = set()
                for rjdx in range(len(tr_enc_exps[p][idx][1])):
                    w_in_re_tar.update(tr_enc_exps[p][idx][1][rjdx])
                tr_inputs.append([1 if ((i < len(word_to_i[p]) and i in w_in_re_src) or
                                        (i >= len(word_to_i[p]) and i - len(word_to_i[p]) in w_in_re_tar)) else 0
                                  for i in range(len(word_to_i[p]) * 2)])
                tr_outputs.append(train[p]["label"][idx])
            te_inputs = []
            for idx in range(len(te_enc_exps[p])):
                w_in_re_src = set()
                for ridx in range(len(te_enc_exps[p][idx][0])):
                    w_in_re_src.update(te_enc_exps[p][idx][0][ridx])
                w_in_re_tar = set()
                for rjdx in range(len(te_enc_exps[p][idx][1])):
                    w_in_re_tar.update(te_enc_exps[p][idx][1][rjdx])
                te_inputs.append([1 if ((i < len(word_to_i[p]) and i in w_in_re_src) or
                                       (i >= len(word_to_i[p]) and i - len(word_to_i[p]) in w_in_re_tar)) else 0
                                 for i in range(len(word_to_i[p]) * 2)])
            print("...... done")
            rs[-1][p] = run_cat_naive_bayes(fs, tr_inputs, train[p]["label"], te_inputs, test[p]["label"],
                                            smooth=1, verbose=verbose)
        print ("... done")

    # Language encoder (train from scratch)
    if args.baseline is None or args.baseline == 'lstm':
        print("Running language encoder lstms...")
        bs.append("Language Encoder")
        rs.append({})
        for p in preps:
            rs[-1][p] = run_lang_2_label(maxlen[p], word_to_i[p],
                                         tr_enc_exps[p], train[p]["label"], te_enc_exps[p], test[p]["label"],
                                         verbose=verbose, batch_size=10000)
        print("... done")

    # Average GLoVe embeddings concatenated and directly used to predict class. (perceptron)
    if args.baseline is None or args.baseline == 'glove':
        print("Preparing infrastructure to run GLoVe-based models...")
        ws = set()
        for p in preps:
            ws.update(word_to_i_all[p].keys())
        g, missing = get_glove_vectors(args.glove_infile, ws)
        emb_dim = len(g[list(g.keys())[0]])
        print("... done; missing " + str(missing) + " vectors out of " + str(len(ws)))

        print("Running GLoVe models")
        bs.extend(["GLoVe Perceptron", "GLoVe 1l", "GLoVe 2l"])
        rs.extend([{}, {}, {}])
        for p in preps:
            tr_f = [[res[train[p][s][idx]] for s in ["ob1", "ob2"]] for idx in
                     range(len(train[p]["ob1"]))]
            te_f = [[res[test[p][s][idx]] for s in ["ob1", "ob2"]] for idx in
                     range(len(test[p]["ob1"]))]
            rs[-3][p] = run_glove_based_model(g, tr_f, train[p]["label"], te_f, test[p]["label"],
                                              epochs=30, verbose=verbose)
            rs[-2][p] = run_glove_based_model(g, tr_f, train[p]["label"], te_f, test[p]["label"],
                                              layers=[emb_dim // 2],
                                              epochs=30, verbose=verbose)
            rs[-1][p] = run_glove_based_model(g, tr_f, train[p]["label"], te_f, test[p]["label"],
                                              layers=[emb_dim // 2, emb_dim // 4],
                                              epochs=30, verbose=verbose)

    # Show results.
    print("Results:")
    for idx in range(len(bs)):
        print(" " + bs[idx] + ":")
        for p in preps:
            print("  " + p + ":\t%0.3f" % rs[idx][p][0])
            print('\t(CM\t' + '\n\t\t'.join(['\t'.join([str(int(ct)) for ct in rs[idx][p][1][i]])
                                             for i in range(len(rs[idx][p][1]))]) + ")")


if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    parser.add_argument('--infile', type=str, required=True,
                        help="input json file with train/dev/test split")
    parser.add_argument('--metadata_infile', type=str, required=True,
                        help="input json file with object metadata")
    parser.add_argument('--baseline', type=str, required=False,
                        help="if None, all will run, else 'majority', 'nb_names', 'nb_bow', 'lstm', 'glove'")
    parser.add_argument('--glove_infile', type=str, required=False,
                        help="input glove vector text file if running glove baseline")
    parser.add_argument('--verbose', type=int, required=False,
                        help="1 if desired")
    main(parser.parse_args())
