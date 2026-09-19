"""Source-time provenance and weak financial-event candidates, never price labels.

An explicit source timezone is required; country is not a timezone inference.
Normalized midnight does not upgrade date-only raw input. Recovered publication
times are candidates for a separately versioned alignment study, not silent edits
to the existing 4813 validation labels. Event matches are review suggestions only.
"""
import hashlib
import re
import unicodedata
from datetime import datetime,timezone,timedelta
from zoneinfo import ZoneInfo,ZoneInfoNotFoundError

CLOCK=re.compile(r'(?:T|\s)\d{1,2}:\d{2}|(?:오전|오후)\s*\d{1,2}:\d{2}')
BAD_METHODS={'source_epoch_placeholder','date_only_invalid_time','unparsed_or_missing'}
EVENTS={
    'earnings':r'실적|영업이익|순이익|매출|\bearnings\b|\brevenue\b|net income|operating profit|quarterly results',
    'contract':r'수주|공급\s*계약|계약을\s*체결|supply agreement|awarded.{0,40}contract|signed.{0,40}contract',
    'merger_acquisition':r'인수(?:합병|했다|한다|를|에|하|할)|합병|\bacquisition\b|\bmerger\b|\bacquire[ds]?\b',
    'capital_return':r'유상\s*증자|자사주|배당|\bbuyback\b|\bdividend\b|share repurchase',
    'legal_operational':r'리콜|소송|파산|생산\s*중단|공장\s*폐쇄|\brecall\b|\blawsuit\b|\bbankruptcy\b|plant closure',
}
EVENT_RE={k:re.compile(v,re.I) for k,v in EVENTS.items()}


def text_digest(text):
    return hashlib.sha256(' '.join(unicodedata.normalize('NFC',text or '').split()).encode('utf8')).hexdigest()


def parse_explicit_time(value,zone=None):
    if not value or not CLOCK.search(str(value)):return None
    try:
        ts=datetime.fromisoformat(str(value).strip().replace('Z','+00:00'))
        if ts.tzinfo is None:
            if not zone:return None
            z=str(zone).strip()
            if z in ('UTC','GMT','Z'):tz=timezone.utc
            elif z=='KST':tz=timezone(timedelta(hours=9))
            elif re.fullmatch(r'(?:UTC|GMT)?[+-]\d{2}:\d{2}',z):
                z=re.sub(r'^(UTC|GMT)','',z);tz=datetime.fromisoformat('2000-01-01T00:00:00'+z).tzinfo
            elif '/' in z:
                tz=ZoneInfo(z)
                first=ts.replace(tzinfo=tz,fold=0);second=ts.replace(tzinfo=tz,fold=1)
                if first.utcoffset()!=second.utcoffset():return None
                if first.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None)!=ts:return None
            else:return None
            ts=ts.replace(tzinfo=tz)
        return ts
    except (ValueError,TypeError,ZoneInfoNotFoundError):return None


def audit_timestamp(row):
    raw=row.get('published_at_raw');normalized=row.get('published_at');date=str(row.get('published_date') or '')[:10]
    method=row.get('date_normalization_method');flags=[]
    collected=parse_explicit_time(row.get('collected_at'))
    base=dict(publication_time_candidate=None,timestamp_status='date_only',collected_time_candidate=collected.astimezone(timezone.utc).isoformat() if collected else None,time_flags=flags)
    if method in BAD_METHODS:base['timestamp_status']='invalid_source_time';return base
    if not CLOCK.search(str(raw or '')):return base
    ts=parse_explicit_time(normalized,row.get('published_timezone'))
    if ts is None:base['timestamp_status']='clock_without_verified_zone';return base
    if ts.date().isoformat()!=date:base['timestamp_status']='date_time_conflict';return base
    if collected:
        if collected<ts:base['timestamp_status']='collection_before_publication';return base
        if (collected-ts).total_seconds()>86400:flags.append('backfill_or_delayed_collection')
    base.update(publication_time_candidate=ts.astimezone(timezone.utc).isoformat(),timestamp_status='explicit_source_time')
    return base


def financial_evidence(title,body,hits):
    """Company and event must occur in the same bounded literal sentence.

    This does not establish subject/object roles or event materiality. Human
    approval is still required; directions and prices are intentionally absent.
    """
    full=(title or '')+'\n'+(body or '');result=[];seen=set()
    for sentence in re.finditer(r'[^\n]+',full):
        # Paragraphs can be huge; split at sentence punctuation followed by space.
        start=sentence.start()
        for part in re.split(r'(?<=[.!?。！？])\s+',sentence.group()):
            local=full.find(part,start,sentence.end());end=local+len(part);start=end
            mentions=[h for h in hits if local<=h['start'] and h['end']<=end]
            if not mentions:continue
            for category,pattern in EVENT_RE.items():
                for event in pattern.finditer(part):
                    e=local+event.start()
                    for h in mentions:
                        if min(abs(e-h['start']),abs(e-h['end']))>180:continue
                        lo=max(local,min(h['start'],e)-80);hi=min(end,max(h['end'],local+event.end())+180)
                        key=(h['ticker'],category,lo,hi)
                        if key in seen:continue
                        seen.add(key);result.append(dict(ticker=h['ticker'],event_type=category,start=lo,end=hi,quote=full[lo:hi],review_status='pending',selection='weak_same_sentence_rule'))
    return result
