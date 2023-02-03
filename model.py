"""
DistilRoberta-base (trained on opentext)
Split cased

This code a slight modification of perplexity by hugging face
https://huggingface.co/docs/transformers/perplexity

Both this code and the orignal code are published under the MIT license.

by Burhan Ul tayyab and Nicholas Chua
"""

import torch
import math
import numpy as np
import random
import re
from transformers import GPT2LMHeadModel, GPT2TokenizerFast
from transformers import pipeline
from transformers import T5ForConditionalGeneration, T5Tokenizer

from collections import OrderedDict

from scipy.stats import norm
from difflib import SequenceMatcher

def similar(a, b):
    return SequenceMatcher(None, a, b).ratio()

def normCdf(x):
    return norm.cdf(x)

def likelihoodRatio(x, y):
    return normCdf(x)/normCdf(y)

# find a better way to abstract the class
class GPT2PPLV2:
    def __init__(self, device="cuda", model_id="gpt2"):
        self.device = device
        self.model_id = model_id
        self.model = GPT2LMHeadModel.from_pretrained(model_id).to(device)
        self.tokenizer = GPT2TokenizerFast.from_pretrained(model_id)

        self.max_length = self.model.config.n_positions
        self.stride = 512
        self.threshold = 60

        self.unmasker = pipeline("fill-mask", model="distilroberta-base", device=0)

    def __call__(self, *args):
        version = args[-1]
        sentence = args[0]
        if version == "v1.1":
            return self.call_1_1(sentence, args[1])
        elif version == "v1":
            return self.call_1(sentence)
        else:
            return "Model version not defined"

################################################
#  Version 1.1 apis
###############################################

    def replaceMask(self, text):
        list_generated_texts = self.unmasker(text)
        output = []
        for generated_texts in list_generated_texts:
            output.append([x["sequence"] for x in generated_texts])
        return output

    def isSame(self, text1, text2):
        return text1 == text2

    def maskRandomWord(self, text, visited):
        words = list(re.finditer("[^\d\W]+", text))
        if len(words) == 0:
            return []

        word_idx = random.randint(0, len(words)-1)
        while word_idx in visited:
            word_idx = random.randint(0, len(words)-1)

        # peform mask filling
        word = words[word_idx]
        mask_text = text
        start, end = word.span()
        mask_text = mask_text[:start] + "<mask>" + mask_text[end:]

        return mask_text, word_idx

    def multiMaskRandomWord(self, texts, listVisited):
        mask_texts = []
        word_indices = []
        for i in range(len(texts)):
            mask_text, word_idx = self.maskRandomWord(texts[i], listVisited[i])
            mask_texts.append(mask_text)
            word_indices.append(word_idx)
        return mask_texts, word_indices

    def multiSetAdd(self, sets, elements):
        for i in range(len(sets)):
            sets[i].add(elements[i])
        return sets

    def chooseBestFittingText(self, texts, original_text):
        for text in texts:
            if text == original_text:
                continue
            return text
        return None

    def multiChooseBestFittingText(self, list_texts, original_text):
        output = []
        for texts in list_texts:
            output.append(self.chooseBestFittingText(texts, original_text))
        return output

    def mask(self, original_text, text, n=2, remaining=100):
        """
        text: string representing the sentence
        n: top n mask-filling to be choosen
        remaining: The remaining slots to be fill
        """

        if remaining <= 0:
            return []

        out_sentences = []
        while remaining > 0: # O(R)
            texts = list(re.finditer("[^\d\W]+", original_text))
            ratio = int(0.15 * len(texts))

            generated_texts = []
            choosen_sets = []
            for _ in range(n):
                generated_texts.append(original_text)
                choosen_sets.append(set())

            for _ in range(ratio):
                mask_texts, word_indices = self.multiMaskRandomWord(generated_texts, choosen_sets)

                list_generated_sentences = self.replaceMask(mask_texts)
                self.multiSetAdd(choosen_sets, word_indices)

                generated_texts = self.multiChooseBestFittingText(list_generated_sentences, original_text)

            out_sentences.extend(generated_texts)
            remaining -= n

        return out_sentences

    def getVerdict(self, score):
        if score < 0.8:
            return "This text is most likely written by an Human"
        elif score < 0.9:
            return "This text could be written by a human"
        elif score < 1.0:
            return "This text could be generated by an A.I."
        else:
            return "This text is most likely generated by an A.I."

    def getScore(self, sentence):
        original_sentence = sentence
        sentence_length = len(list(re.finditer("[^\d\W]+", sentence)))
        print("masking")
        remaining = int(min(max(100, sentence_length * 1/9), 200))
        sentences = self.mask(original_sentence, original_sentence, n=100, remaining=remaining)
        print("masking done")

        real_log_likelihood = self.getLogLikelihood(original_sentence)

        generated_log_likelihoods = []
        for sentence in sentences:
            generated_log_likelihoods.append(self.getLogLikelihood(sentence))
        # print(generated_log_likelihoods)

        if len(generated_log_likelihoods) == 0:
            return -1

        # generate the mean
        mean_generated_log_likelihood = 0.0
        for generated_log_likelihood in generated_log_likelihoods:
            mean_generated_log_likelihood += generated_log_likelihood
        mean_generated_log_likelihood /= len(generated_log_likelihoods)

        var_generated_log_likelihood = 0.0
        # generate the mean
        for generated_log_likelihood in generated_log_likelihoods:
            var_generated_log_likelihood += (generated_log_likelihood - mean_generated_log_likelihood)**2
        var_generated_log_likelihood /= (len(generated_log_likelihoods) - 1)

        diff = real_log_likelihood - mean_generated_log_likelihood

        # print(real_log_likelihood)

        score = diff/var_generated_log_likelihood**(1/2)

        return float(score), float(diff), float(var_generated_log_likelihood**(1/2))

    def call_1_1(self, sentence, chunk_value):
        sentence = re.sub("\[[0-9]+\]", "", sentence) # remove all the [numbers] cause of wiki

        words = re.split("[ \n]", sentence)

        #if len(words) < 100:
            # return {"status": "Please input more text (min 100 words)"}, "Please input more text (min 100 characters)", None

        groups = len(words) // chunk_value + 1
        lines = []
        stride = len(words) // groups + 1
        # print(stride)
        for i in range(0, len(words), stride):
            start_pos = i
            end_pos = min(i+stride, len(words))

            selected_text = " ".join(words[start_pos:end_pos])
            selected_text = selected_text.strip()
            if selected_text == "":
                continue

            lines.append(selected_text)

        # sentence by sentence
        offset = ""
        scores = []
        probs = []
        final_lines = []
        labels = []
        for line in lines:
            if re.search("[a-zA-Z0-9]+", line) == None:
                continue
            if len(offset) > 0:
                line = offset + line
                offset = ""
            # remove the new line pr space in the first sentence if exists
            if line[0] == "\n" or line[0] == " ":
                line = line[1:]
            if line[-1] == "\n" or line[-1] == " ":
                line = line[:-1]
            elif line[-1] == "[" or line[-1] == "(":
                offset = line[-1]
                line = line[:-1]
            score, diff, sd = self.getScore(line)
            if score == -1 or math.isnan(score):
                continue
            scores.append(score)

            final_lines.append(line)
            if score > 0.8:
                labels.append(1)
                prob = "{:.2f}%\n(A.I.)".format(normCdf(abs(0.8 - score)) * 100)
                probs.append(prob)
            else:
                labels.append(0)
                prob = "{:.2f}%\n(Human)".format(normCdf(abs(0.8 - score)) * 100)
                probs.append(prob)

        mean_score = sum(scores)/len(scores)

        mean_prob = normCdf(abs(0.8 - mean_score)) * 100
        label = 0 if mean_score > 0.8 else 1

        print(f"probability for {'A.I.' if label == 0 else 'Human'}:", "{:.2f}%".format(mean_prob))

        return {"prob": "{:.2f}%".format(mean_prob), "label": label}, self.getVerdict(mean_score)

    def getLogLikelihood(self,sentence):
        encodings = self.tokenizer(sentence, return_tensors="pt")
        seq_len = encodings.input_ids.size(1)

        nlls = []
        prev_end_loc = 0
        for begin_loc in range(0, seq_len, self.stride):
            end_loc = min(begin_loc + self.max_length, seq_len)
            trg_len = end_loc - prev_end_loc
            input_ids = encodings.input_ids[:, begin_loc:end_loc].to(self.device)
            target_ids = input_ids.clone()
            target_ids[:, :-trg_len] = -100

            with torch.no_grad():
                outputs = self.model(input_ids, labels=target_ids)

                neg_log_likelihood = outputs.loss * trg_len

            nlls.append(neg_log_likelihood)

            prev_end_loc = end_loc
            if end_loc == seq_len:
                break
        return -1 * torch.stack(nlls).sum() / end_loc

################################################
#  Version 1 apis
###############################################

    def call_1(self, sentence):
        """
        Takes in a sentence split by full stop
        and print the perplexity of the total sentence
        split the lines based on full stop and find the perplexity of each sentence and print
        average perplexity
        Burstiness is the max perplexity of each sentence
        """
        results = OrderedDict()

        total_valid_char = re.findall("[a-zA-Z0-9]+", sentence)
        total_valid_char = sum([len(x) for x in total_valid_char]) # finds len of all the valid characters a sentence

        if total_valid_char < 100:
            return {"status": "Please input more text (min 100 characters)"}, "Please input more text (min 100 characters)"

        lines = re.split(r'(?<=[.?!][ \[\(])|(?<=\n)\s*',sentence)
        lines = list(filter(lambda x: (x is not None) and (len(x) > 0), lines))

        ppl = self.getPPL_1(sentence)
        print(f"Perplexity {ppl}")
        results["Perplexity"] = ppl

        offset = ""
        Perplexity_per_line = []
        for i, line in enumerate(lines):
            if re.search("[a-zA-Z0-9]+", line) == None:
                continue
            if len(offset) > 0:
                line = offset + line
                offset = ""
            # remove the new line pr space in the first sentence if exists
            if line[0] == "\n" or line[0] == " ":
                line = line[1:]
            if line[-1] == "\n" or line[-1] == " ":
                line = line[:-1]
            elif line[-1] == "[" or line[-1] == "(":
                offset = line[-1]
                line = line[:-1]
            ppl = self.getPPL_1(line)
            Perplexity_per_line.append(ppl)
        print(f"Perplexity per line {sum(Perplexity_per_line)/len(Perplexity_per_line)}")
        results["Perplexity per line"] = sum(Perplexity_per_line)/len(Perplexity_per_line)

        print(f"Burstiness {max(Perplexity_per_line)}")
        results["Burstiness"] = max(Perplexity_per_line)

        out, label = self.getResults_1(results["Perplexity per line"])
        results["label"] = label

        return results, out

    def getPPL_1(self,sentence):
        encodings = self.tokenizer(sentence, return_tensors="pt")
        seq_len = encodings.input_ids.size(1)

        nlls = []
        likelihoods = []
        prev_end_loc = 0
        for begin_loc in range(0, seq_len, self.stride):
            end_loc = min(begin_loc + self.max_length, seq_len)
            trg_len = end_loc - prev_end_loc
            input_ids = encodings.input_ids[:, begin_loc:end_loc].to(self.device)
            target_ids = input_ids.clone()
            target_ids[:, :-trg_len] = -100

            with torch.no_grad():
                outputs = self.model(input_ids, labels=target_ids)
                neg_log_likelihood = outputs.loss * trg_len
                likelihoods.append(neg_log_likelihood)

            nlls.append(neg_log_likelihood)

            prev_end_loc = end_loc
            if end_loc == seq_len:
                break
        ppl = int(torch.exp(torch.stack(nlls).sum() / end_loc))
        return ppl

    def getResults_1(self, threshold):
        if threshold < 60:
            label = 0
            return "The Text is generated by AI.", label
        elif threshold < 80:
            label = 0
            return "The Text is most probably contain parts which are generated by AI. (require more text for better Judgement)", label
        else:
            label = 1
            return "The Text is written by Human.", label