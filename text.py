from tqdm import tqdm
import inflect
import csv
import numpy as np
import re
import os
import librosa

def get_symbol_index():
    symbol_dict = {}
    f = open("./symbol_index.txt", "r")
    for line in f:
        x = line.split()
        symbol_dict[x[0]] = int(x[1])
    return symbol_dict

def get_set_words():
    set_words = {}
    with open ('./cmudict.txt', 'r') as f:
        for line in f:
            words = line.split()
            if len(words) > 1:
              set_words[words[0]] = words[1:]
    return set_words

def preprocess_sent(sent):
    sent = sent.upper()
    sent = sent.replace("?", "")
    sent = sent.replace(",", "")
    sent = sent.replace(".", "")
    sent = sent.replace("!", "")
    sent = sent.replace(":", "")
    sent = sent.replace(";", "")
    sent = sent.replace("-", " ")
    sent = sent.replace("(", "")
    sent = sent.replace(")", "")
    sent = sent.replace("\"", "")
    sent = sent.replace("\'", "")
    sent = sent.replace("[", "")
    sent = sent.replace("]", "")
    sent = sent.replace("{", "")
    sent = sent.replace("}", "")
    sent = sent.replace("|", " ")
    sent = sent.replace("À", "A")

    sent = re.sub(r'[0-9]+', '', sent)

    sent = sent.split()
    return sent

def preprocess_text():
    f = open(r"D:\Ziyad\Voice DataSets\LJSpeech-1.1\transcript.csv", "r",encoding='utf-8')
    all_words = get_set_words()
    index_dictionaly = get_symbol_index()

    write = open("train.txt", "w+",encoding='utf-8')
    missing_words = []
    corr_files = []
    files = 0
    idx = 0
    for line in f:
        line = line.split("|")
        id = line[0]
        sent = preprocess_sent(line[2])
        ind = ["84"]
        found = False
        for word in sent:
            if word not in all_words:
                if not found:
                    found = True
                    idx += 1
                missing_words.append(word)
                continue
            for phoneme in all_words[word]:
                ind.append(str(index_dictionaly[phoneme]))
            ind.append("85")
        ind.pop()
        ind.append("86")
        ind.append("87")
        
        
        if found:
            corr_files.append((id,files))
        else:
            write.write(id + "|" + " ".join(ind) + "\n")
        
        
        files += 1
        
    print("Correct files: ", len(corr_files))
    # for file in corr_files:
    #     print(file)

    ## 

    # print("Missing words: ", len(missing_words))
    # missing_set = set(missing_words)
    # for word in missing_set:
    #     print(word)
    # print("Total missing_set: ", len(missing_set))
    # print("Total missing files: ", idx)

    write.close()
    f.close()
preprocess_text()