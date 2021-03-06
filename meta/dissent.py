# -*- coding: utf-8 -*-

"""
DisSent model

Code based on https://github.com/facebookresearch/InferSent/blob/master/models.py
"""

import numpy as np
import time
import logging

import torch
from torch.autograd import Variable
import torch.nn as nn

logger = logging.getLogger(__name__)


def reverse_padded_sequence(inputs, lengths, batch_first=False):
    """Reverses sequences according to their lengths.
    Inputs should have size ``T x B x *`` if ``batch_first`` is False, or
    ``B x T x *`` if True. T is the length of the longest sequence (or larger),
    B is the batch size, and * is any number of dimensions (including 0).
    Arguments:
        inputs (Variable): padded batch of variable length sequences.
        lengths (list[int]): list of sequence lengths
        batch_first (bool, optional): if True, inputs should be B x T x *.
    Returns:
        A Variable with the same size as inputs, but with each sequence
        reversed according to its length.
    """
    if batch_first:
        inputs = inputs.transpose(0, 1)
    max_length, batch_size = inputs.size(0), inputs.size(1)
    if len(lengths) != batch_size:
        raise ValueError('inputs is incompatible with lengths.')
    ind = [list(reversed(range(0, length))) + list(range(length, max_length))
           for length in lengths]
    ind = Variable(torch.LongTensor(ind).transpose(0, 1))
    for dim in range(2, inputs.dim()):
        ind = ind.unsqueeze(dim)
    ind = ind.expand_as(inputs)
    if inputs.is_cuda:
        ind = ind.cuda(inputs.get_device())
    reversed_inputs = torch.gather(inputs, 0, ind)
    if batch_first:
        reversed_inputs = reversed_inputs.transpose(0, 1)
    return reversed_inputs


class BLSTMEncoder(nn.Module):
    def __init__(self, config):
        super(BLSTMEncoder, self).__init__()
        self.bsize = config['bsize']
        self.word_emb_dim = config['word_emb_dim']
        self.enc_lstm_dim = config['enc_lstm_dim']
        self.pool_type = config['pool_type']
        self.dpout_model = config['dpout_model']
        self.dpout_emb = config['dpout_emb']
        self.tied_weights = config['tied_weights']

        bidrectional = True if not self.tied_weights else False

        logger.info("tied weights = {}, using biredictional cell: {}".format(self.tied_weights, bidrectional))
        self.enc_lstm = nn.LSTM(self.word_emb_dim, self.enc_lstm_dim, 1,
                                bidirectional=bidrectional, dropout=self.dpout_model)
        self.emb_drop = nn.Dropout(self.dpout_emb)

    def is_cuda(self):
        # either all weights are on cpu or they are on gpu
        return 'cuda' in str(type(self.enc_lstm.bias_hh_l0.data))

    def forward(self, sent_tuple):
        # sent_len: [max_len, ..., min_len] (bsize)
        # sent: Variable(seqlen x bsize x worddim)
        sent, sent_len = sent_tuple

        # Sort by length (keep idx)
        sent_len, idx_sort = np.sort(sent_len)[::-1], np.argsort(-sent_len)
        idx_unsort = np.argsort(idx_sort)

        idx_sort = torch.from_numpy(idx_sort).cuda() if self.is_cuda() \
            else torch.from_numpy(idx_sort)
        sent = sent.index_select(1, Variable(idx_sort))

        # apply input dropout
        # sent = self.emb_drop(sent)

        # Handling padding in Recurrent Networks
        sent_packed = nn.utils.rnn.pack_padded_sequence(sent, sent_len)
        sent_output = self.enc_lstm(sent_packed)[0]  # seqlen x batch x 2*nhid
        sent_output = nn.utils.rnn.pad_packed_sequence(sent_output)[0]
        if self.tied_weights:
            # we also compute reverse
            sent_rev = reverse_padded_sequence(sent, sent_len)
            sent_rev_packed = nn.utils.rnn.pack_padded_sequence(sent_rev, sent_len)
            rev_sent_output = self.enc_lstm(sent_rev_packed)[0]
            rev_sent_output = nn.utils.rnn.pad_packed_sequence(rev_sent_output)[0]
            back_sent_output = reverse_padded_sequence(rev_sent_output, sent_len)
            sent_output = sent_output + back_sent_output

        # Un-sort by length
        idx_unsort = torch.from_numpy(idx_unsort).cuda() if self.is_cuda() \
            else torch.from_numpy(idx_unsort)
        sent_output = sent_output.index_select(1, Variable(idx_unsort))

        # Pooling
        if self.pool_type == "mean":
            sent_len = Variable(torch.FloatTensor(sent_len)).unsqueeze(1).cuda()
            emb = torch.sum(sent_output, 0).squeeze(0)
            emb = emb / sent_len.expand_as(emb)
        elif self.pool_type == "max":
            emb = torch.max(sent_output, 0)[0]

        return emb

    def set_glove_path(self, glove_path):
        self.glove_path = glove_path

    def get_word_dict(self, sentences, tokenize=True):
        # create vocab of words
        word_dict = {}
        if tokenize:
            from nltk.tokenize import word_tokenize
        sentences = [s.split() if not tokenize else word_tokenize(s)
                     for s in sentences]
        for sent in sentences:
            for word in sent:
                if word not in word_dict:
                    word_dict[word] = ''
        word_dict['<s>'] = ''
        word_dict['</s>'] = ''
        return word_dict

    def get_glove(self, word_dict):
        assert hasattr(self, 'glove_path'), \
            'warning : you need to set_glove_path(glove_path)'
        # create word_vec with glove vectors
        word_vec = {}
        with open(self.glove_path) as f:
            for line in f:
                word, vec = line.split(' ', 1)
                if word in word_dict:
                    word_vec[word] = np.fromstring(vec, sep=' ')
        print('Found {0}(/{1}) words with glove vectors'.format(
            len(word_vec), len(word_dict)))
        return word_vec

    def get_glove_k(self, K):
        assert hasattr(self, 'glove_path'), 'warning : you need \
                                             to set_glove_path(glove_path)'
        # create word_vec with k first glove vectors
        k = 0
        word_vec = {}
        with open(self.glove_path) as f:
            for line in f:
                word, vec = line.split(' ', 1)
                if k <= K:
                    word_vec[word] = np.fromstring(vec, sep=' ')
                    k += 1
                if k > K:
                    if word in ['<s>', '</s>']:
                        word_vec[word] = np.fromstring(vec, sep=' ')

                if k > K and all([w in word_vec for w in ['<s>', '</s>']]):
                    break
        return word_vec

    def build_vocab(self, sentences, tokenize=True):
        assert hasattr(self, 'glove_path'), 'warning : you need \
                                             to set_glove_path(glove_path)'
        word_dict = self.get_word_dict(sentences, tokenize)
        self.word_vec = self.get_glove(word_dict)
        print('Vocab size : {0}'.format(len(self.word_vec)))

    # build GloVe vocab with k most frequent words
    def build_vocab_k_words(self, K):
        assert hasattr(self, 'glove_path'), 'warning : you need \
                                             to set_glove_path(glove_path)'
        self.word_vec = self.get_glove_k(K)
        print('Vocab size : {0}'.format(K))

    def update_vocab(self, sentences, tokenize=True):
        assert hasattr(self, 'glove_path'), 'warning : you need \
                                             to set_glove_path(glove_path)'
        assert hasattr(self, 'word_vec'), 'build_vocab before updating it'
        word_dict = self.get_word_dict(sentences, tokenize)

        # keep only new words
        for word in self.word_vec:
            if word in word_dict:
                del word_dict[word]

        # udpate vocabulary
        if word_dict:
            new_word_vec = self.get_glove(word_dict)
            self.word_vec.update(new_word_vec)
        print('New vocab size : {0} (added {1} words)'.format(
            len(self.word_vec), len(new_word_vec)))

    def get_batch(self, batch):
        # sent in batch in decreasing order of lengths
        # batch: (bsize, max_len, word_dim)
        embed = np.zeros((len(batch[0]), len(batch), self.word_emb_dim))

        for i in range(len(batch)):
            for j in range(len(batch[i])):
                embed[j, i, :] = self.word_vec[batch[i][j]]

        return torch.FloatTensor(embed)

    def prepare_samples(self, sentences, bsize, tokenize, verbose):
        if tokenize:
            from nltk.tokenize import word_tokenize
        sentences = [['<s>'] + s.split() + ['</s>'] if not tokenize else
                     ['<s>'] + word_tokenize(s) + ['</s>'] for s in sentences]
        n_w = np.sum([len(x) for x in sentences])

        # filters words without glove vectors
        for i in range(len(sentences)):
            s_f = [word for word in sentences[i] if word in self.word_vec]
            if not s_f:
                import warnings
                warnings.warn('No words in "{0}" (idx={1}) have glove vectors. \
                               Replacing by "</s>"..'.format(sentences[i], i))
                s_f = ['</s>']
            sentences[i] = s_f

        lengths = np.array([len(s) for s in sentences])
        n_wk = np.sum(lengths)
        if verbose:
            print('Nb words kept : {0}/{1} ({2} %)'.format(
                n_wk, n_w, round((100.0 * n_wk) / n_w, 2)))

        # sort by decreasing length
        lengths, idx_sort = np.sort(lengths)[::-1], np.argsort(-lengths)
        sentences = np.array(sentences)[idx_sort]

        return sentences, lengths, idx_sort

    def encode(self, sentences, bsize=64, tokenize=True, verbose=False):
        tic = time.time()
        sentences, lengths, idx_sort = self.prepare_samples(
            sentences, bsize, tokenize, verbose)

        embeddings = []
        for stidx in range(0, len(sentences), bsize):
            batch = Variable(self.get_batch(
                sentences[stidx:stidx + bsize]), volatile=True)
            if self.is_cuda():
                batch = batch.cuda()
            batch = self.forward(
                (batch, lengths[stidx:stidx + bsize])).data.cpu().numpy()
            embeddings.append(batch)
        embeddings = np.vstack(embeddings)

        # unsort
        idx_unsort = np.argsort(idx_sort)
        embeddings = embeddings[idx_unsort]

        if verbose:
            print('Speed : {0} sentences/s ({1} mode, bsize={2})'.format(
                round(len(embeddings) / (time.time() - tic), 2),
                'gpu' if self.is_cuda() else 'cpu', bsize))
        return embeddings


class DisSent(nn.Module):
    def __init__(self, config):
        super(DisSent, self).__init__()

        # classifier
        self.fc_dim = config['fc_dim']
        self.n_classes = config['n_classes']
        self.enc_lstm_dim = config['enc_lstm_dim']
        self.encoder_type = config['encoder_type']
        self.dpout_fc = config['dpout_fc']
        self.tied_weights = config['tied_weights']
        self.distance = config['distance']

        self.encoder = eval(self.encoder_type)(config)
        self.inputdim = 5 * 2 * self.enc_lstm_dim if not self.tied_weights else 5 * self.enc_lstm_dim

        # It's meta-learning setting, no final classifier
        # in github, they also didn't use dropout:
        # https://github.com/renmengye/few-shot-ssl-public/search?utf8=%E2%9C%93&q=dropout
        self.classifier = nn.Sequential(
            nn.Linear(self.inputdim, self.fc_dim),
            nn.Linear(self.fc_dim, self.fc_dim)  # not sure if we should have this layer...
        )
        # No non-linearity because we don't want "feature-selection"
        # can understand ReLU or Sigmoid as feature select
        # we want every dimension of vector calculus concatenation to get gradient

        # hypothesis: information compression happens without activation function
        # when rank is reduced (dimension reduction). But still what is middle layer doing?

        # set up similarity function
        # for L2 we use negative distance (like the paper)
        # can't use lambda function cause it won't pickle!!
        self.b, self.c = 0., 0.

    def l2_vec(self, x, p):
        return -(torch.sum(x * x, dim=1).view(-1, 1).expand(-1, self.c) + \
                 torch.sum(p * p, dim=0).view(1, -1).expand(self.b, -1) \
                 + 2 * torch.matmul(x, p))

    def cos_vec(self, x, p):
        return torch.matmul(x, p) / torch.ger(x.norm(2, dim=1), p.norm(2, dim=0))

    def dot_vec(self, x, p):
        return torch.matmul(x, p)

    def build_proto_vecs(self, sent_reps, target_to_batch_idx, num_uniq_y):
        # target_to_batch_idx: {0: np.array([1, 5, 6]), 1: np.array([2, 3]), ...}
        # sent_reps: (batch_size, fc_dim)

        # average of vectors
        proto_vecs = [torch.sum(sent_reps[target_to_batch_idx[i], :], 0) / target_to_batch_idx[i].size for i in
                      xrange(num_uniq_y)]
        proto_vecs = torch.stack(proto_vecs, dim=1)
        # (fc_dim, num_uniq_y)
        return proto_vecs

    def forward(self, s1, s2, target_to_batch_idx, num_uniq_y, batch_size):
        # s1 : (s1, s1_len)
        u = self.encoder(s1)
        v = self.encoder(s2)

        features = torch.cat((u, v, u - v, u * v, (u + v) / 2.), 1)
        sent_reps = self.classifier(features)  # (batch_size, fc_dim)

        proto_vecs = self.build_proto_vecs(sent_reps, target_to_batch_idx, num_uniq_y)

        # we want outside to be completely the same
        # these two are constantly updated
        self.b = batch_size
        self.c = num_uniq_y

        if self.distance == 'l2':
            # nicely, self.b and self.c will dynamically change
            # in forward() method
            y_hat = self.l2_vec(sent_reps, proto_vecs)
        elif self.distance == 'cos':
            y_hat = self.cos_vec(sent_reps, proto_vecs)
        elif self.distance == 'dot':
            y_hat = self.dot_vec(sent_reps, proto_vecs)
        else:
            raise Exception("unrecognizable distance configuration. Choose from l2|cos|dot")

        # y_hat = self.sim_fn(sent_reps, proto_vecs)  # should be (batch_size, num_uniq_y)

        return y_hat

    def encode(self, s1):
        emb = self.encoder(s1)
        return emb
