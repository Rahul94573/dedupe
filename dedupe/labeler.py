from __future__ import division
from future.utils import with_metaclass

import random
from abc import ABCMeta, abstractmethod

import numpy
import rlr

import dedupe.sampling as sampling
import dedupe.core as core
import dedupe.training as training
import dedupe.blocking as blocking

class ActiveLearner(with_metaclass(ABCMeta)):

    @abstractmethod
    def transform():
        pass

    @abstractmethod
    def pop():
        pass

    @abstractmethod
    def mark():
        pass

    @abstractmethod
    def __len__(self):
        pass

    def sample_combo(self, data, blocked_proportion, sample_size):
        blocked_sample_size = int(blocked_proportion * sample_size)
        predicates = list(self.data_model.predicates(index_predicates=False))

        data = sampling.randomDeque(data)
        blocked_sample_keys = sampling.dedupeBlockedSample(blocked_sample_size,
                                                           predicates,
                                                           data)

        random_sample_size = sample_size - len(blocked_sample_keys)
        random_sample_keys = set(core.randomPairs(len(data),
                                                  random_sample_size))
        data = dict(data)

        return [(data[k1], data[k2])
                for k1, k2
                in blocked_sample_keys | random_sample_keys]
        

    def sample_product(self, data_1, data_2, blocked_proportion, sample_size):
        offset = len(data_1)

        blocked_sample_size = int(blocked_proportion * sample_size)
        predicates = list(self.data_model.predicates(index_predicates=False))

        deque_1 = sampling.randomDeque(data_1)
        deque_2 = sampling.randomDeque(data_2)

        blocked_sample_keys = sampling.linkBlockedSample(blocked_sample_size,
                                                         predicates,
                                                         deque_1,
                                                         deque_2)

        random_sample_size = sample_size - len(blocked_sample_keys)
        random_sample_keys = core.randomPairsMatch(len(deque_1),
                                                   len(deque_2),
                                                   random_sample_size)

        random_sample_keys = {(a, b + offset)
                              for a, b in random_sample_keys}

        return [(data_1[k1], data_2[k2])
                for k1, k2
                in blocked_sample_keys | random_sample_keys]

            

class RLRLearner(ActiveLearner, rlr.RegularizedLogisticRegression):
    def __init__(self, data_model):
        super(RLRLearner, self).__init__(alpha=1)
        
        self.data_model = data_model
        
    def transform(self, pairs):
        return self.data_model.distances(pairs)

    def fit(self, X, y):
        self.y = numpy.array(y)
        self.X = X

        super(RLRLearner, self).fit(self.X, self.y, cv=False)

    def fit_transform(self, pairs, y):
        self.fit(self.transform(pairs), y)

    def pop(self):
        if not len(self.candidates):
            raise IndexError("No more unlabeled examples to label")

        target_uncertainty = self._bias()

        probabilities = self.candidate_scores()
        uncertain_index = numpy.abs(target_uncertainty - probabilities).argmin()

        self.distances = numpy.delete(self.distances, uncertain_index, axis=0)

        uncertain_pair = self.candidates.pop(uncertain_index)

        return [uncertain_pair]

    def remove(self, candidate):
        index = self.candidates.index(candidate)
        self.distances = numpy.delete(self.distances, index, axis=0)
        self.candidates.pop(index)

    def mark(self, pairs, y):

        self.y = numpy.concatenate([self.y, y])
        self.X = numpy.vstack([self.X, self.transform(pairs)])

        self.fit(self.X, self.y)

    def _bias(self):
        positive = numpy.sum(self.y == 1)
        n_examples = len(self.y)

        bias = 1 - (positive/n_examples if positive else 0)

        # When we have just a few examples we are okay with getting
        # examples where the model strongly believes the example is
        # going to be positive or negative. As we get more examples,
        # prefer to ask for labels of examples the model is more
        # uncertain of.
        uncertainty_weight = min(positive, n_examples - positive)
        bias_weight = 10

        weighted_bias = 0.5 * uncertainty_weight + bias * bias_weight
        weighted_bias /= uncertainty_weight + bias_weight

        return weighted_bias

    def candidate_scores(self):
        return self.predict_proba(self.distances)

    def __len__(self):
        return len(self.candidates)

    def _init(self, candidates, *args):
        # we should rethink this and have it happen in the __init__ method
        self.candidates = candidates.copy()
        self.distances = self.transform(candidates)
        random_pair = random.choice(self.candidates)
        exact_match = (random_pair[0], random_pair[0])
        self.fit_transform([exact_match, random_pair],
                           [1, 0])
        
    def sample_combo(self, *args):
        candidates = super(RLRLearner, self).sample_combo(*args)
        self._init(candidates)

    def sample_product(self, *args):
        candidates = super(RLRLearner, self).sample_product(*args)
        self._init(candidates)


class DedupeBlockLearner(object):
    def __init__(self, data_model):
        self.blocker = blocking.Blocker(data_model.predicates(index_predicates=False))
        self.old_predicates = None

    def fit_transform(self, pairs, y):
        dupes = [pair for label, pair in zip(y, pairs) if label]
        self.current_predicates = self.block_learner.learn(dupes, recall=1.0)
        print(self.current_predicates)
        print(self.current_predicates == self.old_predicates)
        self.old_predicates = self.current_predicates
        
    def candidate_scores(self):
        labels = []
        for record_1, record_2 in self.candidates:
            for predicate in self.current_predicates:
                keys = predicate(record_1)
                if keys:
                    if set(predicate(record_2)) & set(keys):
                        labels.append(1)
                        break
            else:
                labels.append(0)

        return numpy.array(labels).reshape(-1, 1)

    def _init(self, candidates, sampled_records):
        self.blocker.indexAll(sampled_records)
        self.block_learner = training.DedupeBlockLearner(self.blocker.predicates, sampled_records)
        self.candidates = candidates.copy()

    def remove(self, candidate):
        index = self.candidates.index(candidate)
        self.candidates.pop(index)
    
        

class DisagreementLearner(ActiveLearner):
    learners = (RLRLearner, DedupeBlockLearner)
    
    def __init__(self, data_model):
        self.data_model = data_model
        self.learners = [learner(data_model) for learner in self.learners]
        self.y = numpy.array([])
        self.pairs = []
    

        
    def pop(self):
        if not len(self.candidates):
            raise IndexError("No more unlabeled examples to label")

        probs = []
        for learner in self.learners:
            probabilities = learner.candidate_scores()
            probs.append(probabilities)

        disagreement =  numpy.std(numpy.concatenate(probs, axis=1), axis=1)

        uncertain_index = disagreement.argmax()

        print(probs[0][uncertain_index], probs[1][uncertain_index])

        uncertain_pair = self.candidates.pop(uncertain_index)

        for learner in self.learners:
            learner.remove(uncertain_pair)

        return [uncertain_pair]

    def mark(self, pairs, y):

        self.y = numpy.concatenate([self.y, y])
        self.pairs.extend(pairs)

        for learner in self.learners:
            learner.fit_transform(self.pairs, self.y)

    def __len__(self):
        return len(self.candidates)

    def sample(self, data, blocked_proportion, sample_size, original_length=None):
       # get the sample for canidates and pass it to all the learners
        
        data = core.index(data)


        self.candidates = self.sample_combo(data, blocked_proportion, sample_size)

        if original_length is None:
            original_length = len(data)

        sampled_records = Sample(data, 2000, original_length)

        for learner in self.learners:
            learner._init(self.candidates, sampled_records)

    def transform(self):
        pass


class Sample(dict):

    def __init__(self, d, sample_size, original_length):
        if len(d) <= sample_size:
            super(Sample, self).__init__(d)
        else:
            super(Sample, self).__init__({k: d[k]
                                          for k
                                          in random.sample(d.keys(),
                                                           sample_size)})
        self.original_length = original_length


