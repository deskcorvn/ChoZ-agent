# Catalog retrieval (app/kb.py), offline

Recall@1 / Recall@3 / MRR on queries whose product is in the catalog; out-of-catalog queries (24) are not scored here: matching or abstaining is the verifier's decision, measured live.

| Retriever | Query kind | n | Recall@1 | Recall@3 | MRR |
|---|---|---|---|---|---|
| bm25 | all | 112 | 111/112 | 112/112 | 0.996 |
| bm25 | combined | 27 | 27/27 | 27/27 | 1.000 |
| bm25 | no_diacritics | 23 | 23/23 | 23/23 | 1.000 |
| bm25 | noisy_speech | 23 | 23/23 | 23/23 | 1.000 |
| bm25 | photo_phrase_en | 10 | 9/10 | 10/10 | 0.950 |
| bm25 | seller_words | 5 | 5/5 | 5/5 | 1.000 |
| bm25 | speech | 6 | 6/6 | 6/6 | 1.000 |
| bm25 | title | 18 | 18/18 | 18/18 | 1.000 |
| trigram | all | 112 | 108/112 | 112/112 | 0.982 |
| trigram | combined | 27 | 27/27 | 27/27 | 1.000 |
| trigram | no_diacritics | 23 | 22/23 | 23/23 | 0.978 |
| trigram | noisy_speech | 23 | 22/23 | 23/23 | 0.978 |
| trigram | photo_phrase_en | 10 | 9/10 | 10/10 | 0.950 |
| trigram | seller_words | 5 | 5/5 | 5/5 | 1.000 |
| trigram | speech | 6 | 6/6 | 6/6 | 1.000 |
| trigram | title | 18 | 17/18 | 18/18 | 0.972 |
| hybrid | all | 112 | 108/112 | 112/112 | 0.982 |
| hybrid | combined | 27 | 27/27 | 27/27 | 1.000 |
| hybrid | no_diacritics | 23 | 22/23 | 23/23 | 0.978 |
| hybrid | noisy_speech | 23 | 22/23 | 23/23 | 0.978 |
| hybrid | photo_phrase_en | 10 | 9/10 | 10/10 | 0.950 |
| hybrid | seller_words | 5 | 5/5 | 5/5 | 1.000 |
| hybrid | speech | 6 | 6/6 | 6/6 | 1.000 |
| hybrid | title | 18 | 17/18 | 18/18 | 0.972 |
| bm25+fallback | all | 112 | 111/112 | 112/112 | 0.996 |
| bm25+fallback | combined | 27 | 27/27 | 27/27 | 1.000 |
| bm25+fallback | no_diacritics | 23 | 23/23 | 23/23 | 1.000 |
| bm25+fallback | noisy_speech | 23 | 23/23 | 23/23 | 1.000 |
| bm25+fallback | photo_phrase_en | 10 | 9/10 | 10/10 | 0.950 |
| bm25+fallback | seller_words | 5 | 5/5 | 5/5 | 1.000 |
| bm25+fallback | speech | 6 | 6/6 | 6/6 | 1.000 |
| bm25+fallback | title | 18 | 18/18 | 18/18 | 1.000 |

Misses at rank 1 of the retriever in use (bm25+fallback):

- photo_phrase_en: “instant noodle cup” gold kt-ca-ro-hop at rank 2, top ['kt-cua-hop', 'kt-ca-ro-hop', 'kt-chao-hau-hop']
