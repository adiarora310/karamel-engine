# Lexicons

`vader_lexicon.txt` is VADER's sentiment lexicon, unmodified, from
https://github.com/cjhutto/vaderSentiment (`vaderSentiment/vader_lexicon.txt`),
fetched 2026-10-18, 7,520 entries, 426,786 bytes. MIT licence, copyright
(c) 2016 C.J. Hutto: see `VADER_LICENSE.txt`, which must ship with it.

Hutto & Gilbert, "VADER: A Parsimonious Rule-based Model for Sentiment
Analysis of Social Media Text", ICWSM 2014.

Used by `critic.has_feeling`, and only in reaction position: a strongly rated
word in a sentence of four words or fewer, or just after the author
("I'm obsessed", "kinda heartbreaking"). On its own VADER scores what a post is
about ("fraud", "survived") rather than how the author felt, which is why it is
not used as a plain threshold.

EmoLex and LIWC were considered and are not here: both need a paid licence
for commercial use.
