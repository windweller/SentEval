# Copyright (c) 2017-present, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
#

from __future__ import absolute_import, division, unicode_literals

import sys
import os
import torch
import argparse
from exutil import dotdict
import logging
import numpy as np
from os.path import join as pjoin

reload(sys)
sys.setdefaultencoding('utf-8')

parser = argparse.ArgumentParser(description='DisSent SentEval Evaluation')
parser.add_argument("--outputdir", type=str, default='sandbox/', help="Output directory")
parser.add_argument("--outputmodelname", type=str, default='dis-model')
parser.add_argument("--gpu_id", type=int, default=0, help="GPU ID, we map all model's gpu to this id")
parser.add_argument("--best_epoch", type=int, default=9, help="Directly indicate the best epoch")

params, _ = parser.parse_known_args()

# Set PATHs
GLOVE_PATH = '/home/anie/glove/glove.840B.300d.txt'
PATH_SENTEVAL = '/home/anie/SentEval'
PATH_TO_DATA = '/home/anie/SentEval/data/senteval_data/'
MODEL_PATH = 'infersent.allnli.pickle'

assert os.path.isfile(MODEL_PATH) and os.path.isfile(GLOVE_PATH), \
    'Set MODEL and GloVe PATHs'

# import senteval
sys.path.insert(0, PATH_SENTEVAL)
import senteval


def prepare(params, samples):
    params.infersent.build_vocab([' '.join(s) for s in samples],
                                 tokenize=False)
    params.dissent.build_vocab([' '.join(s) for s in samples],
                                 tokenize=False)


def batcher(params, batch):
    # batch contains list of words
    sentences = [' '.join(s) for s in batch]
    infersent_embeddings = params.infersent.encode(sentences, bsize=params.batch_size,
                                         tokenize=False)
    dissent_embeddings = params.dissent.encode(sentences, bsize=params.batch_size,
                                         tokenize=False)

    embeddings = np.hstack([dissent_embeddings, infersent_embeddings])

    return embeddings


"""
Evaluation of trained model on Transfer Tasks (SentEval)
"""

# define transfer tasks
transfer_tasks = ['MR', 'CR', 'SUBJ', 'MPQA', 'SST', 'TREC', 'SICKRelatedness',
                  'SICKEntailment', 'MRPC', 'STS14']

# define senteval params
params_senteval = dotdict({'usepytorch': True, 'task_path': PATH_TO_DATA,
                           'seed': 1111, 'kfold': 5})

# Set up logger
logging.basicConfig(format='%(asctime)s : %(message)s', level=logging.DEBUG)

if __name__ == "__main__":
    map_locations = {}
    for d in range(4):
        if d != 0:
            map_locations['cuda:{}'.format(d)] = "cuda:{}".format(0)

    # load Dis model
    model_name = params.outputmodelname + '-{}.pickle'.format(params.best_epoch)
    model_path = pjoin(params.outputdir, model_name)
    dissent = torch.load(model_path, map_location=map_locations)
    params_senteval.dissent = dissent.encoder
    params_senteval.dissent.set_glove_path(GLOVE_PATH)

    # Load InferSent model
    params_senteval.infersent = torch.load(MODEL_PATH)
    params_senteval.infersent.set_glove_path(GLOVE_PATH)

    se = senteval.SentEval(params_senteval, batcher, prepare)
    results_transfer = se.eval(transfer_tasks)

    print(results_transfer)
