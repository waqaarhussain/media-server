"""Shared, non-personal related-film ranking from genres, keywords and synopsis text."""
from collections import Counter
import math
import re
import unicodedata

STOP=set('''a an the and or of to in on at for from with without by into over after before as is are was were be been being his her their its he she they them him it this that these those who whom when while where which what how not but only also must has have had does do did will would can could one two young man woman life lives find finds new story film movie about becomes become takes take set world together get gets'''.split())

GENERIC_KEYWORDS={'based on novel or book','based on novel','based on book',
                  'aftercreditsstinger','duringcreditsstinger','independent film','woman director'}


def normal(value):
    return ' '.join(re.findall(r'[a-z0-9]+',unicodedata.normalize('NFKD',str(value or '')).encode('ascii','ignore').decode().lower()))


def keywords(movie):
    values=movie.get('keywords') or []
    return {normal(v.get('name') if isinstance(v,dict) else v) for v in values if isinstance(v,(str,dict))}-{''}-GENERIC_KEYWORDS


def genres(movie):
    return {normal(g) for g in movie.get('genres') or [] if isinstance(g,str)}


def tokens(movie):
    return Counter(w for w in normal(movie.get('overview')).split() if len(w)>2 and w not in STOP)


def related_ids(data,seed_id):
    if not isinstance(data,dict) or data.get('TmdbId',data.get('tmdbId'))!=seed_id:
        return []
    result=[]
    for item in data.get('Recommendations',data.get('recommendations',[])) or []:
        if not isinstance(item,dict):continue
        tid=item.get('TmdbId',item.get('tmdbId'))
        if type(tid) is int and tid>0 and tid!=seed_id and tid not in result:result.append(tid)
    return result[:24]


def rank(seed,candidates):
    # Candidate membership is supplied by the metadata provider for this exact
    # movie ID. Local matching refines that relation; it never infers a franchise
    # from similar title words or uses any viewer's personal/download history.
    seed_genres=genres(seed);seed_keywords=keywords(seed)
    documents=[tokens(seed)]+[tokens(m) for m in candidates]
    df=Counter(word for doc in documents for word in doc)
    idf={word:math.log((len(documents)+1)/(count+1))+1 for word,count in df.items()}
    def vector(doc):return {w:(1+math.log(n))*idf[w] for w,n in doc.items()}
    def cosine(a,b):
        denominator=math.sqrt(sum(v*v for v in a.values())*sum(v*v for v in b.values()))
        return sum(v*b.get(w,0) for w,v in a.items())/denominator if denominator else 0
    origin=vector(documents[0]);result=[];seen={seed.get('tmdbId')}
    for index,movie in enumerate(candidates):
        tid=movie.get('tmdbId')
        if not tid or tid in seen or not movie.get('title'):continue
        seen.add(tid)
        common_genres=seed_genres&genres(movie);common_keys=seed_keywords&keywords(movie)
        genre_score=len(common_genres)/max(1,len(seed_genres|genres(movie)))
        key_score=len(common_keys)/max(1,len(seed_keywords|keywords(movie)))
        plot_score=cosine(origin,vector(documents[index+1]))
        score=.30*genre_score+.35*key_score+.25*plot_score+.10/(1+index/4)
        reasons=[]
        if common_keys:reasons.append('Shared themes: '+', '.join(sorted(common_keys)[:3]))
        if common_genres:reasons.append('Shared genres: '+', '.join(sorted(common_genres)[:3]))
        if plot_score>=.12:reasons.append('Similar plot-description language')
        if not reasons:reasons.append('Related-film suggestion from Radarr metadata')
        result.append(dict(movie,similar_score=score,similar_reasons=reasons))
    return sorted(result,key=lambda m:(-m['similar_score'],m['tmdbId']))
