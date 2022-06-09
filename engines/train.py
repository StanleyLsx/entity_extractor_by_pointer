# -*- coding: utf-8 -*-
# @Author : lishouxian
# @Email : gzlishouxian@gmail.com
# @File : train.py
# @Software: PyCharm
from engines.models.BinaryPointer import BinaryPointer
from transformers import AdamW
from tqdm import tqdm
from torch.utils.data import DataLoader
import json
import torch
import time
import os


class Train:
    def __init__(self, configs, data_manager, device, logger):
        self.configs = configs
        self.device = device
        self.logger = logger
        self.data_manager = data_manager
        self.batch_size = self.configs['batch_size']
        self.num_labels = len(configs['classes'])

        learning_rate = configs['learning_rate']

        if configs['model_type'] == 'bp':
            from engines.models.BinaryPointer import BinaryPointer
            self.model = BinaryPointer(num_labels=self.num_labels).to(device)

        else:
            from engines.models.GlobalPointer import EffiGlobalPointer
            self.model = EffiGlobalPointer(num_labels=self.num_labels, device=device).to(device)

        params = list(self.model.parameters())
        self.optimizer = AdamW(params, lr=learning_rate)

        if configs['use_multilabel_categorical_cross_entropy']:
            from engines.utils.losses import MultilabelCategoricalCrossEntropy
            self.loss_function = MultilabelCategoricalCrossEntropy()
        else:
            self.loss_function = torch.nn.BCEWithLogitsLoss(reduction='none')

    def train(self):
        train_file = self.configs['train_file']
        dev_file = self.configs['dev_file']
        train_data = json.load(open(train_file, encoding='utf-8'))
        dev_data = json.load(open(dev_file, encoding='utf-8'))
        self.logger.info('loading train data...')
        train_loader = DataLoader(
            dataset=train_data,
            batch_size=self.batch_size,
            collate_fn=self.data_manager.prepare_data,
            shuffle=True
        )
        dev_loader = DataLoader(
            dataset=dev_data,
            batch_size=self.batch_size,
            collate_fn=self.data_manager.prepare_data,
        )
        best_f1 = 0
        best_epoch = 0
        unprocessed = 0
        very_start_time = time.time()

        for i in range(self.configs['epoch']):
            self.logger.info('\nepoch:{}/{}'.format(i + 1, self.configs['epoch']))
            self.model.train()
            start_time = time.time()
            step, loss, loss_sum = 0, 0.0, 0.0
            for batch in tqdm(train_loader):
                _, _, token_ids, token_type_ids, attention_mask, label_vectors = batch
                batch_size = token_ids.size(0)
                token_ids = token_ids.to(self.device)
                attention_mask = attention_mask.to(self.device)
                token_type_ids = token_type_ids.to(self.device)
                label_vectors = label_vectors.to(self.device)
                self.optimizer.zero_grad()
                logits, _ = self.model(token_ids, attention_mask, token_type_ids)

                if self.configs['use_multilabel_categorical_cross_entropy']:
                    if self.configs['model_type'] == 'bp':
                        num_labels = self.num_labels * 2
                    else:
                        num_labels = self.num_labels
                    model_output = logits.reshape(batch_size * num_labels, -1)
                    label_vectors = label_vectors.reshape(batch_size * num_labels, -1)
                    loss = self.loss_function(model_output, label_vectors)
                else:
                    if self.configs['model_type'] == 'bp':
                        loss = self.loss_function(logits, label_vectors)
                        loss = torch.sum(torch.mean(loss, 3), 2)
                        loss = torch.sum(loss * attention_mask) / torch.sum(attention_mask)
                    else:
                        model_output = logits.reshape(batch_size * self.num_labels, -1)
                        label_vectors = label_vectors.reshape(batch_size * self.num_labels, -1)
                        loss = self.loss_function(model_output, label_vectors).mean()

                loss.backward()
                loss_sum += loss.item()
                self.optimizer.step()

                if step % self.configs['print_per_batch'] == 0 and step != 0:
                    avg_loss = loss_sum / (step + 1)
                    self.logger.info('training_loss:%f' % avg_loss)

                step = step + 1

            f1 = self.validate(dev_loader)
            time_span = (time.time() - start_time) / 60
            self.logger.info('time consumption:%.2f(min)' % time_span)
            if f1 >= best_f1:
                unprocessed = 0
                best_f1 = f1
                best_epoch = i + 1
                torch.save(self.model.state_dict(), os.path.join(self.configs['checkpoints_dir'], 'best_model.pkl'))
                self.logger.info('saved model successful...')
            else:
                unprocessed += 1
            aver_loss = loss_sum / step
            self.logger.info(
                'aver_loss: %.4f, f1: %.4f, best_f1: %.4f, best_epoch: %d \n' % (aver_loss, f1, best_f1, best_epoch))
            if self.configs['is_early_stop']:
                if unprocessed > self.configs['patient']:
                    self.logger.info('early stopped, no progress obtained within {} epochs'.format(
                        self.configs['patient']))
                    self.logger.info('overall best f1 is {} at {} epoch'.format(best_f1, best_epoch))
                    self.logger.info('total training time consumption: %.3f(min)' % (
                            (time.time() - very_start_time) / 60))
                    return
        self.logger.info('overall best f1 is {} at {} epoch'.format(best_f1, best_epoch))
        self.logger.info('total training time consumption: %.3f(min)' % ((time.time() - very_start_time) / 60))

    def validate(self, dev_loader):
        counts = {}
        results_of_each_entity = {}
        for class_name, class_id in self.data_manager.categories.items():
            counts[class_id] = {'A': 0.0, 'B': 1e-10, 'C': 1e-10}
            class_name = self.data_manager.reverse_categories[class_id]
            results_of_each_entity[class_name] = {}

        with torch.no_grad():
            self.model.eval()
            self.logger.info('start evaluate engines...')
            for batch in tqdm(dev_loader):
                texts, entity_results, token_ids, segment_ids, attention_mask, _ = batch
                token_ids = token_ids.to(self.device)
                segment_ids = segment_ids.to(self.device)
                attention_mask = attention_mask.to(self.device)
                logits, _ = self.model(token_ids, attention_mask, segment_ids)
                logits = logits.to('cpu')
                for text, logit, entity_result in zip(texts, logits, entity_results):
                    p_results = self.data_manager.extract_entities(text, logit)
                    for class_id, entity_set in entity_result.items():
                        p_entity_set = p_results.get(class_id)
                        if p_entity_set is None:
                            # 没预测出来
                            p_entity_set = set()
                        # 预测出来并且正确个数
                        counts[class_id]['A'] += len(p_entity_set & entity_set)
                        # 预测出来的结果个数
                        counts[class_id]['B'] += len(p_entity_set)
                        # 真实的结果个数
                        counts[class_id]['C'] += len(entity_set)
        for class_id, count in counts.items():
            f1, precision, recall = 2 * count['A'] / (
                    count['B'] + count['C']), count['A'] / count['B'], count['A'] / count['C']
            class_name = self.data_manager.reverse_categories[class_id]
            results_of_each_entity[class_name]['f1'] = f1
            results_of_each_entity[class_name]['precision'] = precision
            results_of_each_entity[class_name]['recall'] = recall

        f1 = 0.0
        for class_id, performance in results_of_each_entity.items():
            f1 += performance['f1']
            # 打印每个类别的指标
            self.logger.info('class_name: %s, precision: %.4f, recall: %.4f, f1: %.4f'
                        % (class_id, performance['precision'], performance['recall'], performance['f1']))
        # 这里算得是所有类别的平均f1值
        f1 = f1 / len(results_of_each_entity)
        return f1
