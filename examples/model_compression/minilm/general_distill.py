# Copyright (c) 2021 PaddlePaddle Authors. All Rights Reserved.
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

import argparse
import os
import random
import time
from functools import partial
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import paddle
from paddle.io import DataLoader

from paddlenlp.utils.log import logger
from paddlenlp.data import Tuple, Pad
from paddlenlp.utils.tools import TimeCostAverage
from paddlenlp.transformers import LinearDecayWithWarmup
from paddlenlp.transformers import TinyBertForPretraining, TinyBertTokenizer
from paddlenlp.transformers import RobertaModel, RobertaTokenizer

from paddlenlp.transformers.distill_utils import to_distill, calc_minilm_loss

MODEL_CLASSES = {
    "roberta": (RobertaModel, RobertaTokenizer),
    "tinybert": (TinyBertForPretraining, TinyBertTokenizer),
}


def parse_args():
    parser = argparse.ArgumentParser()

    # Required parameters
    parser.add_argument(
        "--model_type",
        default="tinybert",
        type=str,
        required=True,
        help="Model type selected in the list: " +
        ", ".join(MODEL_CLASSES.keys()), )
    parser.add_argument(
        "--teacher_model_type",
        default="bert",
        type=str,
        required=True,
        help="Model type selected in the list: " +
        ", ".join(MODEL_CLASSES.keys()), )
    parser.add_argument(
        "--student_model_name_or_path",
        default=None,
        type=str,
        required=True,
        help="Path to pre-trained model or shortcut name selected in the list: "
        + ", ".join(
            sum([
                list(classes[-1].pretrained_init_configuration.keys())
                for classes in MODEL_CLASSES.values()
            ], [])), )
    parser.add_argument(
        "--teacher_model_name_or_path",
        default=None,
        type=str,
        required=True,
        help="Path to pre-trained model.")
    parser.add_argument(
        "--input_dir",
        default=None,
        type=str,
        required=True,
        help="The input directory where the data will be read from.", )
    parser.add_argument(
        "--max_predictions_per_seq",
        default=80,
        type=int,
        help="The maximum total of masked tokens in input sequence")
    parser.add_argument(
        "--output_dir",
        default=None,
        type=str,
        required=True,
        help="The output directory where the model predictions and checkpoints will be written.",
    )

    parser.add_argument(
        "--max_seq_length",
        default=128,
        type=int,
        help="The maximum total input sequence length after tokenization. Sequences longer "
        "than this will be truncated, sequences shorter will be padded.", )
    parser.add_argument(
        "--learning_rate",
        default=6e-4,
        type=float,
        help="The initial learning rate for Adam.")
    parser.add_argument(
        "--max_grad_norm", default=1.0, type=float, help="Max gradient norm.")
    parser.add_argument(
        "--num_train_epochs",
        default=3,
        type=int,
        help="Total number of training epochs to perform.", )
    parser.add_argument(
        "--logging_steps",
        type=int,
        default=100,
        help="Log every X updates steps.")
    parser.add_argument(
        "--save_steps",
        type=int,
        default=100,
        help="Save checkpoint every X updates steps.")
    parser.add_argument(
        "--batch_size",
        default=512,
        type=int,
        help="Batch size per GPU/CPU for training.", )
    parser.add_argument(
        "--num_relation_heads",
        default=64,
        type=int,
        help="The number of relation heads is 48 and 64 for base and large-size teacher model.",
    )
    parser.add_argument(
        "--weight_decay",
        default=0.01,
        type=float,
        help="Weight decay if we apply some.")
    parser.add_argument(
        "--warmup_steps",
        default=-1,
        type=int,
        help="Linear warmup over warmup_steps. If > 0: Override warmup_proportion"
    )
    parser.add_argument(
        "--warmup_proportion",
        default=0.01,
        type=float,
        help="Linear warmup proportion over total steps.")
    parser.add_argument(
        "--adam_epsilon",
        default=1e-8,
        type=float,
        help="Epsilon for Adam optimizer.")
    parser.add_argument(
        "--max_steps",
        default=400000,
        type=int,
        help="If > 0: set total number of training steps to perform. Override num_train_epochs.",
    )
    parser.add_argument(
        "--seed", default=42, type=int, help="random seed for initialization")
    parser.add_argument(
        "--device",
        default="gpu",
        type=str,
        help="The device to select to train the model, is must be cpu/gpu/xpu.")
    args = parser.parse_args()
    return args


def set_seed(args):
    random.seed(args.seed + paddle.distributed.get_rank())
    np.random.seed(args.seed + paddle.distributed.get_rank())
    paddle.seed(args.seed + paddle.distributed.get_rank())


class WorkerInitObj(object):
    def __init__(self, seed):
        self.seed = seed

    def __call__(self, id):
        np.random.seed(seed=self.seed + id)
        random.seed(self.seed + id)


def create_pretraining_dataset(input_file, max_pred_length, shared_list, args,
                               worker_init, tokenizer):
    train_data = PretrainingDataset(
        input_file=input_file,
        max_pred_length=max_pred_length,
        tokenizer=tokenizer,
        max_seq_length=args.max_seq_length)
    # files have been sharded, no need to dispatch again
    train_batch_sampler = paddle.io.BatchSampler(
        train_data, batch_size=args.batch_size, shuffle=True)

    # DataLoader cannot be pickled because of its place.
    # If it can be pickled, use global function instead of lambda and use
    # ProcessPoolExecutor instead of ThreadPoolExecutor to prefetch.
    batchify_fn = lambda samples, fn=Tuple(
        Pad(axis=0, pad_val=tokenizer.pad_token_id),  # input
    ): fn(samples)

    train_data_loader = DataLoader(
        dataset=train_data,
        batch_sampler=train_batch_sampler,
        collate_fn=batchify_fn,
        num_workers=0,
        worker_init_fn=worker_init,
        return_list=True)
    return train_data_loader, input_file


class PretrainingDataset(paddle.io.Dataset):
    def __init__(self, input_file, max_pred_length, tokenizer, max_seq_length):
        self.input_file = input_file
        self.max_pred_length = max_pred_length
        f = open(input_file, 'r')
        input_ids = []
        for i, line in enumerate(f):
            line = line[:max_seq_length]
            tokenized_example = tokenizer(line, max_seq_len=max_seq_length)
            input_ids.append(tokenized_example['input_ids'])
        self.inputs = np.asarray(input_ids)
        f.close()

    def __len__(self):
        'Denotes the total number of samples'
        return len(self.inputs)

    def __getitem__(self, index):
        input_ids = [np.asarray(self.inputs[index])]
        return input_ids


def do_train(args):
    paddle.set_device(args.device)
    if paddle.distributed.get_world_size() > 1:
        paddle.distributed.init_parallel_env()

    set_seed(args)

    worker_init = WorkerInitObj(args.seed + paddle.distributed.get_rank())
    args.model_type = args.model_type.lower()

    # For student
    model_class, tokenizer_class = MODEL_CLASSES[args.model_type]
    tokenizer = tokenizer_class.from_pretrained(args.student_model_name_or_path)
    student = model_class.from_pretrained(args.student_model_name_or_path)

    # For teacher
    teacher_model_class, _ = MODEL_CLASSES[args.teacher_model_type]
    teacher = teacher_model_class.from_pretrained(
        args.teacher_model_name_or_path)
    pad_token_id = student.pretrained_init_configuration[
        args.student_model_name_or_path]['pad_token_id']
    if paddle.distributed.get_world_size() > 1:
        student = paddle.DataParallel(student, find_unused_parameters=True)
        teacher = paddle.DataParallel(teacher, find_unused_parameters=True)

    num_training_steps = args.max_steps

    warmup = args.warmup_steps if args.warmup_steps > 0 else args.warmup_proportion
    lr_scheduler = LinearDecayWithWarmup(args.learning_rate, num_training_steps,
                                         warmup)
    optimizer = paddle.optimizer.Adam(
        learning_rate=lr_scheduler,
        beta1=0.9,
        beta2=0.98,
        epsilon=args.adam_epsilon,
        parameters=student.parameters(),
        weight_decay=args.weight_decay)

    pool = ThreadPoolExecutor(1)

    teacher = to_distill(teacher, return_qkv=True, layer_index=18)
    student = to_distill(student, return_qkv=True, layer_index=5)

    global_step = 0
    tic_train = time.time()
    for epoch in range(args.num_train_epochs):
        files = [
            os.path.join(args.input_dir, f) for f in os.listdir(args.input_dir)
            if os.path.isfile(os.path.join(args.input_dir, f))
        ]
        files.sort()
        num_files = len(files)
        random.Random(args.seed + epoch).shuffle(files)
        f_start_id = 0

        shared_file_list = {}

        if paddle.distributed.get_world_size() > num_files:
            remainder = paddle.distributed.get_world_size() % num_files

            data_file = files[(
                f_start_id * paddle.distributed.get_world_size() +
                paddle.distributed.get_rank() + remainder * f_start_id) %
                              num_files]
        else:
            data_file = files[(f_start_id * paddle.distributed.get_world_size()
                               + paddle.distributed.get_rank()) % num_files]

        previous_file = data_file

        train_data_loader, _ = create_pretraining_dataset(
            data_file, args.max_predictions_per_seq, shared_file_list, args,
            worker_init, tokenizer)

        # TODO(guosheng): better way to process single file
        single_file = True if f_start_id + 1 == len(files) else False

        for f_id in range(f_start_id, len(files)):
            if not single_file and f_id == f_start_id:
                continue
            if paddle.distributed.get_world_size() > num_files:
                data_file = files[(
                    f_id * paddle.distributed.get_world_size() +
                    paddle.distributed.get_rank() + remainder * f_id) %
                                  num_files]
            else:
                data_file = files[(f_id * paddle.distributed.get_world_size() +
                                   paddle.distributed.get_rank()) % num_files]
            previous_file = data_file
            dataset_future = pool.submit(create_pretraining_dataset, data_file,
                                         args.max_predictions_per_seq,
                                         shared_file_list, args, worker_init,
                                         tokenizer)

            kl_loss_fct = paddle.nn.KLDivLoss('sum')
            train_cost_avg = TimeCostAverage()
            total_samples = 0
            batch_start = time.time()
            for step, batch in enumerate(train_data_loader):
                global_step += 1
                input_ids = batch[0]
                attention_mask = paddle.unsqueeze(
                    (input_ids == pad_token_id
                     ).astype(paddle.get_default_dtype()) * -1e9,
                    axis=[1, 2])
                student(input_ids, attention_mask=attention_mask)
                with paddle.no_grad():
                    teacher(input_ids, attention_mask=attention_mask)
                # Q-Q relation
                q_t, q_s = teacher.outputs.q, student.outputs.q
                batch_size = q_t.shape[0]
                pad_seq_len = q_t.shape[2]
                loss_qr = calc_minilm_loss(kl_loss_fct, q_s, q_t,
                                           attention_mask,
                                           args.num_relation_heads)
                del q_t, q_s
                # K-K relation
                k_t, k_s = teacher.outputs.k, student.outputs.k
                loss_kr = calc_minilm_loss(kl_loss_fct, k_s, k_t,
                                           attention_mask,
                                           args.num_relation_heads)
                del k_t, k_s
                # V-V relation
                v_t, v_s = teacher.outputs.v, student.outputs.v
                loss_vr = calc_minilm_loss(kl_loss_fct, v_s, v_t,
                                           attention_mask,
                                           args.num_relation_heads)

                del v_t, v_s

                loss = loss_qr + loss_kr + loss_vr
                loss /= args.num_relation_heads * pad_seq_len * batch_size
                loss.backward()

                optimizer.step()
                lr_scheduler.step()
                optimizer.clear_grad()

                total_samples += args.batch_size
                train_run_cost = time.time() - batch_start
                train_cost_avg.record(train_run_cost)
                if global_step % args.logging_steps == 0:
                    logger.info(
                        "global step: %d, epoch: %d, batch: %d, loss: %f, "
                        "lr: %f, avg_batch_cost: %.5f sec, avg_samples: %.5f, ips: %.5f sequences/sec"
                        % (global_step, epoch, step, loss, optimizer.get_lr(),
                           train_cost_avg.get_average(),
                           total_samples / args.logging_steps, total_samples /
                           (args.logging_steps * train_cost_avg.get_average())))
                    total_samples = 0
                    train_cost_avg.reset()
                if global_step % args.save_steps == 0 or global_step == num_training_steps:
                    if paddle.distributed.get_rank() == 0:
                        output_dir = os.path.join(args.output_dir,
                                                  "model_%d" % global_step)
                        if not os.path.exists(output_dir):
                            os.makedirs(output_dir)
                        # need better way to get inner model of DataParallel
                        model_to_save = student._layers if isinstance(
                            student, paddle.DataParallel) else student
                        model_to_save.save_pretrained(output_dir)
                        tokenizer.save_pretrained(output_dir)
                        paddle.save(
                            optimizer.state_dict(),
                            os.path.join(output_dir, "model_state.pdopt"))
                if global_step >= args.max_steps:
                    del train_data_loader
                    return
                batch_start = time.time()

            del train_data_loader
            train_data_loader, data_file = dataset_future.result(timeout=None)


def print_arguments(args):
    """print arguments"""
    print('-----------  Configuration Arguments -----------')
    for arg, value in sorted(vars(args).items()):
        print('%s: %s' % (arg, value))
    print('------------------------------------------------')


if __name__ == "__main__":
    args = parse_args()
    print_arguments(args)
    do_train(args)
