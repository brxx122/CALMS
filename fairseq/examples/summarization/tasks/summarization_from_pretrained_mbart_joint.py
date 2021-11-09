# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import os
import torch
from argparse import Namespace
import json
import itertools
import logging
import os

import numpy as np

from fairseq import metrics, options, utils
from fairseq.data import (
    AppendTokenDataset,
    ConcatDataset,
    data_utils,
    encoders,
    indexed_dataset,
    LanguagePairDataset,
    PrependTokenDataset,
    StripTokenDataset,
    TruncateDataset,
    ResamplingDataset,
    SortDataset
)

from fairseq.tasks.translation_from_pretrained_bart import TranslationFromPretrainedBARTTask
from fairseq.tasks import register_task

logger = logging.getLogger(__name__)

def load_langpair_sumdataset(
    data_path, split,
    src, src_dict, src_lang,
    tgt, tgt_dict, tgt_lang,
    combine, dataset_impl, upsample_primary,
    left_pad_source, left_pad_target, max_source_positions,
    max_target_positions, prepend_bos=False, load_alignments=False,
    truncate_source=False, append_source_id=False, trg_prepend=False,
):

    def split_exists(split, src, tgt, lang, data_path):
        filename = os.path.join(data_path, '{}.{}-{}.{}'.format(split, src, tgt, lang))
        return indexed_dataset.dataset_exists(filename, impl=dataset_impl)

    src_datasets = []
    tgt_datasets = []

    for k in itertools.count():
        split_k = split + (str(k) if k > 0 else '')

        # infer langcode
        if split_exists(split_k, src, tgt, src, data_path):
            prefix = os.path.join(data_path, '{}.{}-{}.'.format(split_k, src, tgt))
        elif split_exists(split_k, tgt, src, src, data_path):
            prefix = os.path.join(data_path, '{}.{}-{}.'.format(split_k, tgt, src))
        else:
            if k > 0:
                break
            else:
                raise FileNotFoundError('Dataset not found: {} ({})'.format(split, data_path))

        src_dataset = data_utils.load_indexed_dataset(prefix + src, src_dict, dataset_impl)
        src_datasets.append(src_dataset)

        tgt_dataset = data_utils.load_indexed_dataset(prefix + tgt, tgt_dict, dataset_impl)
        if tgt_dataset is not None:
            tgt_datasets.append(tgt_dataset)

        logger.info('{} {} {}-{} {} examples'.format(
            data_path, split_k, src, tgt, len(src_datasets[-1])
        ))

        if not combine:
            break

    assert len(src_datasets) == len(tgt_datasets) or len(tgt_datasets) == 0

    if len(src_datasets) == 1:
        src_dataset = src_datasets[0]
        tgt_dataset = tgt_datasets[0] if len(tgt_datasets) > 0 else None
    else:
        sample_ratios = [1] * len(src_datasets)
        sample_ratios[0] = upsample_primary
        src_dataset = ConcatDataset(src_datasets, sample_ratios)
        if len(tgt_datasets) > 0:
            tgt_dataset = ConcatDataset(tgt_datasets, sample_ratios)
        else:
            tgt_dataset = None

    if prepend_bos:
        assert hasattr(src_dict, "bos_index") and hasattr(tgt_dict, "bos_index")
        src_dataset = PrependTokenDataset(src_dataset, src_dict.bos())
        if tgt_dataset is not None:
            tgt_dataset = PrependTokenDataset(tgt_dataset, tgt_dict.bos())

    if truncate_source:
        trunc_len = max_source_positions-1 if append_source_id else max_source_positions
        logger.info("Truncate source to max length %d", trunc_len)
        src_dataset = AppendTokenDataset(
            TruncateDataset(
                StripTokenDataset(src_dataset, src_dict.eos()),
                trunc_len - 1,
            ),
            src_dict.eos(),
        )

    eos = None
    if append_source_id:
        src_dataset = AppendTokenDataset(src_dataset, src_dict.index('[{}]'.format(src_lang)))
        if tgt_dataset is not None:
            if trg_prepend:
                tgt_dataset = PrependTokenDataset(tgt_dataset, tgt_dict.index('[{}]'.format(tgt_lang)))
            tgt_dataset = AppendTokenDataset(tgt_dataset, tgt_dict.index('[{}]'.format(tgt_lang)))
        eos = tgt_dict.index('[{}]'.format(tgt_lang))
    

    align_dataset = None
    if load_alignments:
        align_path = os.path.join(data_path, '{}.align.{}-{}'.format(split, src, tgt))
        if indexed_dataset.dataset_exists(align_path, impl=dataset_impl):
            align_dataset = data_utils.load_indexed_dataset(align_path, None, dataset_impl)

    tgt_dataset_sizes = tgt_dataset.sizes if tgt_dataset is not None else None
    return LanguagePairDataset(
        src_dataset, src_dataset.sizes, src_dict,
        tgt_dataset, tgt_dataset_sizes, tgt_dict,
        left_pad_source=left_pad_source,
        left_pad_target=left_pad_target,
        max_source_positions=max_source_positions,
        max_target_positions=max_target_positions,
        align_dataset=align_dataset, eos=eos
    )

@register_task('summarization_from_pretrained_mbart_joint')
class SummarizationFromPretrainedMBARTTaskJoint(TranslationFromPretrainedBARTTask):
    """
    Translate from source language to target language with a model initialized with a multilingual pretrain.

    Args:
        src_dict (~fairseq.data.Dictionary): dictionary for the source language
        tgt_dict (~fairseq.data.Dictionary): dictionary for the target language

    .. note::

        The translation task is compatible with :mod:`fairseq-train`,
        :mod:`fairseq-generate` and :mod:`fairseq-interactive`.

    The translation task provides the following additional command-line
    arguments:

    .. argparse::
        :ref: fairseq.tasks.translation_parser
        :prog:
    """

    @staticmethod
    def add_args(parser):
        """Add task-specific arguments to the parser."""
        # fmt: off
        TranslationFromPretrainedBARTTask.add_args(parser)
        parser.add_argument('--doc-lang', help='document language (only for inference)')
        parser.add_argument('--sum-lang', help='summary language (only for inference)')
        parser.add_argument('--langs-for-sum', required=True, help='language for summary pretrain')
        parser.add_argument('--fix2x', default='', help='one language to other languages')
        parser.add_argument('--trg-prepend', action='store_true', help='fixed decoder during from pretrain')
        # fmt: on

    def __init__(self, args, src_dict, tgt_dict):
        super().__init__(args, src_dict, tgt_dict)
        logger.info("bos %d, pad %d, eos %d, unk %d", 
                src_dict.index('<s>'),src_dict.index('<pad>'),
                src_dict.index('</s>'),src_dict.index('<unk>')
                )
        self.langs_for_summ = args.langs_for_sum.split(",")
        

    def load_dataset(self, split, epoch=1, combine=False, **kwargs):
        """Load a given dataset split.

        Args:
            split (str): name of the split (e.g., train, valid, test)
        """
        paths = self.args.data.split(':')
        assert len(paths) > 0
        data_path = paths[(epoch - 1) % len(paths)]

        # src="doc", tgt="sum"
        src, tgt = self.args.source_lang, self.args.target_lang

        lang_datasets= []
        languages = self.langs_for_summ
        for lang in languages:
            code = lang.split('_')[0]   # en_XX -> en
            lang_path = os.path.join(data_path, code)
            logger.info("load lang {} from {}".format(lang, lang_path))
            if self.args.fix2x != '':
                srclang = self.args.fix2x
            else:
                srclang = lang
            lang_dataset = load_langpair_sumdataset(
                lang_path, split, 
                src, self.src_dict, srclang,
                tgt, self.tgt_dict, lang,
                combine=combine, dataset_impl=self.args.dataset_impl,
                upsample_primary=self.args.upsample_primary,
                left_pad_source=self.args.left_pad_source,
                left_pad_target=self.args.left_pad_target,
                max_source_positions=getattr(self.args, 'max_source_positions', 1024),
                max_target_positions=getattr(self.args, 'max_target_positions', 1024),
                truncate_source=self.args.truncate_source,
                load_alignments=self.args.load_alignments,
                prepend_bos=getattr(self.args, 'preprend_bos', False),
                append_source_id=True, trg_prepend=getattr(self.args, 'trg_prepend', False),
                )
            lang_datasets.append(lang_dataset)
            # print(lang_dataset[0])

        dataset_lengths = np.array(
            [len(d) for d in lang_datasets],
            dtype=float,
        )
        logger.info(
            'Loaded total {} examples for all languages'.format(
                dataset_lengths.sum(),
            )
        )

        dataset = ConcatDataset(lang_datasets)
        lang_splits = [split]
        for lang_id, lang_dataset in enumerate(lang_datasets):
            split_name = split + '_' + languages[lang_id]
            lang_splits.append(split_name)
            self.datasets[split_name] = lang_dataset

        if split in self.args.valid_subset:
            self.args.valid_subset = self.args.valid_subset.replace(
                split, ','.join(lang_splits)
            )

        with data_utils.numpy_seed(self.args.seed + epoch):
            shuffle = np.random.permutation(len(dataset))

        self.datasets[split] = SortDataset(
            dataset,
            sort_order=[
                shuffle,
                dataset.sizes,
            ],
        )

        self.datasets[split] = dataset
        print(self.datasets[split][0])

    def build_model(self, args):
        model = super().build_model(args)
        
        return model


    def build_generator(self, models, args):
        if getattr(args, 'score_reference', False):
            from fairseq.sequence_scorer import SequenceScorer
            return SequenceScorer(
                self.target_dictionary,
                eos=self.tgt_dict.index('[{}]'.format(self.args.sum_lang))
            )
        else:
            from fairseq.sequence_generator import SequenceGenerator
            return SequenceGenerator(
                models,
                self.target_dictionary,
                beam_size=getattr(args, 'beam', 5),
                max_len_a=getattr(args, 'max_len_a', 0),
                max_len_b=getattr(args, 'max_len_b', 200),
                min_len=getattr(args, 'min_len', 1),
                normalize_scores=(not getattr(args, 'unnormalized', False)),
                len_penalty=getattr(args, 'lenpen', 1),
                unk_penalty=getattr(args, 'unkpen', 0),
                temperature=getattr(args, 'temperature', 1.),
                match_source_len=getattr(args, 'match_source_len', False),
                no_repeat_ngram_size=getattr(args, 'no_repeat_ngram_size', 0),
                eos=self.tgt_dict.index('[{}]'.format(self.args.sum_lang))  # eos: beginning of sentence token
            )

    def build_dataset_for_inference(self, src_tokens, src_lengths):
        src_lang_id = self.source_dictionary.index('[{}]'.format(self.args.doc_lang))
        source_tokens = []
        for s_t in src_tokens:
            s_t = torch.cat([s_t, s_t.new(1).fill_(src_lang_id)])
            source_tokens.append(s_t)
        dataset = LanguagePairDataset(source_tokens, src_lengths, self.source_dictionary)
        return dataset

    def _get_sample_prob(self, dataset_lens):
        """
        Get smoothed sampling porbability by languages. This helps low resource
        languages by upsampling them.
        """
        prob = dataset_lens / dataset_lens.sum()
        # smoothed_prob = prob ** self.args.multilang_sampling_alpha
        smoothed_prob = prob ** 1.0 # self.args.multilang_sampling_alpha=1.0
        smoothed_prob = smoothed_prob / smoothed_prob.sum()
        return smoothed_prob
