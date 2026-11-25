"""Reviewer scratch checks for task 1f (offline, synthetic data). Run: python docs/review_1f_checks.py
Not part of the test suite; prints observations quoted in docs/review_1f.md."""
import sys; sys.path.insert(0,'.')
from dataclasses import replace
from collections import Counter
import random, json, hashlib
from datetime import *
from zoneinfo import ZoneInfo
from src.config import load_config, tier_for, next_working_windows, window_bounds
from src.models import *
from src.ranker import *
from src.ranker import _persona_rank, _age_days
cfg=load_config()
NOW=datetime(2026,11,25,20,0,tzinfo=timezone.utc)
def acct(i="A1",emp=500,tz="Australia/Sydney",name=None,**kw): return Account(sf_account_id=f"F-{i}",name=name or f"Acct {i}",employees=emp,timezone=tz,**kw)
def sig(i="A1",k=SignalKind.FUNDING,s=1.0,days=0,src=None,date_=True):
    return Signal(f"F-{i}",k,s,src or f"s{i}{k.value}{days}",NOW-timedelta(days=days) if date_ else None,"e")
def pr(i="A1",title="Head of Product",phone="+61400000001",name="Pat",**kw):
    return Prospect(nooks_id=f"P{i}{name}",sf_contact_id=None,sf_account_id=f"F-{i}",nooks_account_id=None,name=name,title=title,phone_e164=phone,**kw)
print("== scenarios")
# S1: strength .8, 10 days old, tier2(100), prospect, sdr, no cooldown
a=acct(emp=100,is_sdr_target=True); print("S1",score_account(a,[sig(s=.8,days=9)],True,False,cfg,NOW))
# S2: no signal, tier1, no prospect, cooldown, flagged
a=acct(emp=2000,non_anz_billing_anz_hq=True); print("S2",score_account(a,[],False,True,cfg,NOW))
# S3: 3 signals, tier1, prospect, sdr -> clamp
a=acct(emp=200,is_sdr_target=True); print("S3",score_account(a,[sig(s=.95,days=0),sig(k=SignalKind.MA,s=.9,days=0),sig(k=SignalKind.NEW_HIRE,s=.5,days=3)],True,False,cfg,NOW))
print("== tiers")
for e in [200,199,50,49,20,19,2000,2001,0,-5,None]:
    try: print(e,tier_for(cfg,e), score_account(acct(emp=e),[],False,False,cfg,NOW)[1]["icp_fit"])
    except Exception as ex: print(e,"EXC",repr(ex))
print("== recency")
for d in [0,1,22,45,46]: print(d,score_account(acct(),[sig(days=d)],False,False,cfg,NOW)[1]["recency"])
print("future", score_account(acct(),[sig(days=-10)],False,False,cfg,NOW)[1]["recency"], "nodate",score_account(acct(),[sig(date_=False)],False,False,cfg,NOW)[1]["recency"])
# naive datetime source_date?
try:
    s=Signal("F-A1",SignalKind.FUNDING,.5,"n",datetime(2026,11,20),"e"); print(score_account(acct(),[s],False,False,cfg,NOW))
except Exception as ex: print("naive source_date EXC",repr(ex))
print("== personas")
titles=["Chief Technology Officer","CTO & Co-founder","Head of Product & Design","VP, Product","Product Manager (Growth)","Digital Transformation Manager","Senior Product Manager","Customer Success Director","Head of CX","Group Head of Digital Workplace","",None,"Director","Director of Product Management","Chief Product Officer","VP Product","VP of Product","Vice President, Product","Vice President of Product","Head of Product, ANZ","Head of Products","Product Managers","Chief Product & Technology Officer","Head of Customer Experience","Head of Customer Success ANZ","Head Of Product","Director, Product","Director of Product Management","Product Owner","Head of Digital Product","Chief Digital & Information Officer","Chief Information Officer","Head of Engineering","Engineering Manager","Director of Engineering","Chief Executive Officer","Head of Customer Support","Head of Digital Transformation","Group Product Manager","Principal Product Manager","Director of Customer Experience","VP Customer Success","GM Product","Chief Digital Officer","Head of Digital Experience","CTO","Co-CTO","Product Director","Digital Director","Head of Technology","Group CTO","Head of IT Operations","Head of Information Technology","Product Lead","Director of Product-Led Growth"]
for t in titles+['Chief Product & Technology Officer','Product Owner','Engineering Manager','Head of Digital Experience','Customer Success Manager','Director, Product Management','Product-Led Growth Lead','Head of Product-Led Growth']: print(repr(t), _persona_rank(t,cfg))
# ---------------------------------------------------------------- re-review (commit 28980ed)
from dataclasses import replace
from collections import Counter
import random, json, hashlib, itertools
def run(accts,sigs={},ps=(),cool={},last=set(),now=NOW): return rank_accounts(accts,sigs,list(ps),cool,last,cfg,now)
def dump(r): return json.dumps([(x.account.sf_account_id,x.score,x.window,str(x.local_call_time),x.reasons) for x in r.ranked]+[(x.account.sf_account_id,x.skipped_reason) for x in r.skipped]+[(x.account.sf_account_id,x.score,x.reasons) for x in r.needs_prospect]+sorted(r.needs_prospect_codes.items()),default=str)
rnd=random.Random(5)
accts=[acct(str(i),emp=rnd.choice([10,30,100,500,None]),tz=rnd.choice(["Australia/Sydney","Australia/Perth","Pacific/Auckland",None]),timezone_guessed=rnd.random()<.3,is_sdr_target=rnd.random()<.2) for i in range(150)]
sigs={f"F-{i}":[sig(str(i),s=rnd.choice([.5,.8]),days=rnd.choice([1,5,30]))] for i in range(0,150,2)}
ps=[pr(str(i),name=f"N{i}") for i in range(0,150,3)]
base=dump(run(accts,sigs,ps)); print("DIGEST",hashlib.md5(base.encode()).hexdigest())
ok=True
for seed in range(20):
    r2=random.Random(seed); a=accts[:]; p=ps[:]; r2.shuffle(a); r2.shuffle(p)
    ok&=dump(run(a,{k:random.Random(seed).sample(v,len(v)) for k,v in sigs.items()},p))==base
print("shuffle determinism",ok)
print("== H1 45 no-prospect vs 10 callable")
nop=[acct(f"NP{i}",name=f"NP{i:02d}") for i in range(45)]; cal=[acct(f"C{i}",name=f"C{i:02d}",emp=30) for i in range(10)]
r=run(nop+cal,{f"F-NP{i}":[sig(f"NP{i}")] for i in range(45)},[pr(f"C{i}",name=f"c{i}") for i in range(10)])
print("callable ranked",len(r.ranked),"needs_prospect",len(r.needs_prospect),"any window/time on needs:",any(x.window or x.local_call_time for x in r.needs_prospect),"codes",Counter(r.needs_prospect_codes.values()))
print(summarise(r).splitlines()[:6])
r=run([acct("a"),acct("b"),acct("c"),acct("d")],{},[pr("b",title="Intern"),pr("c",title="Head of Product",phone=None),pr("d",title="Head of Product",do_not_call=True)])
print({k:v for k,v in r.needs_prospect_codes.items()},[(s.account.name,s.skipped_reason) for s in r.skipped])
print("== H2 evening run Tue 20:00 AEDT, cooldown_until=Wed (call date) / Thu")
now=datetime(2026,11,24,20,0,tzinfo=ZoneInfo("Australia/Sydney")).astimezone(timezone.utc)
for cu in (date(2026,11,25),date(2026,11,26)):
    x=run([acct("E")],{},[pr("E")],{"F-E":cu},set(),now).ranked[0]; print(cu,x.local_call_time,x.score_breakdown["cooldown_penalty"])
x=run([acct("E")],{},[pr("E")],{"F-E":datetime(2026,11,25,tzinfo=timezone.utc)},set(),now).ranked[0]; print("datetime ok",x.score_breakdown["cooldown_penalty"])
for bad in ("2026-11-25",5):
    try: run([acct("E")],{},[pr("E")],{"F-E":bad},set(),now)
    except Exception as e: print("bad type",repr(e)[:80])
now=datetime(2026,11,25,23,0,tzinfo=timezone.utc); print("23:00 UTC, cooldown_until Thu(local call date):",run([acct("E")],{},[pr("E")],{"F-E":date(2026,11,26)},set(),now).ranked[0].score_breakdown["cooldown_penalty"])
print("== M1 last sheet"); r=run([acct("T",name="Top"),acct("O",emp=30)],{"F-T":[sig("T")]},[pr("T"),pr("O",name="o")],{},{"F-T"}); print([x.account.name for x in r.ranked],[s.skipped_reason for s in r.skipped])
r=run([acct("T")],{},[],{},{"F-T"}); print("needs_prospect + last sheet ->",len(r.needs_prospect),[s.skipped_reason for s in r.skipped])
print("== M3 bad tz")
for tz in ("Mars/Base","",None): print(repr(tz),[s.skipped_reason for s in run([acct("Z",tz=tz)],{},[pr("Z")]).skipped])
print("== M5/scheduling")
def times(r): return [x.local_call_time for x in r.ranked]
def gaps(ts): ts=sorted(ts); return min((b-a).total_seconds() for a,b in zip(ts,ts[1:])) if len(ts)>1 else None
accts=[acct(f"G{i}",name=f"G{i:02d}") for i in range(40)]; P=[pr(f"G{i}",name=f"g{i}") for i in range(40)]
r=run(accts,{},P); ts=times(r); print("40 Sydney, 20:00UTC (07:00 Thu): min gap s",gaps(ts),min(ts),max(ts),"windows",{k:len(v) for k,v in r.by_window.items()},"spilled",sum('Spilled' in ' '.join(x.reasons) for x in r.ranked))
for lbl,loc in [("Thu 15:00 Syd",datetime(2026,11,26,15,0,tzinfo=ZoneInfo("Australia/Sydney"))),("Thu 11:29 Syd",datetime(2026,11,26,11,29,tzinfo=ZoneInfo("Australia/Sydney"))),("Thu 16:28 Syd",datetime(2026,11,26,16,28,tzinfo=ZoneInfo("Australia/Sydney"))),("Fri 15:00 Syd",datetime(2026,11,27,15,0,tzinfo=ZoneInfo("Australia/Sydney")))]:
    r=run(accts,{},P,now=loc.astimezone(timezone.utc)); ts=times(r); sp=[x for x in r.ranked if any('Spilled' in y for y in x.reasons)]
    print(lbl,"ranked",len(r.ranked),"min gap s",gaps(ts),"first",min(ts).strftime('%a %H:%M'),"last",max(ts).strftime('%a %d %H:%M'),"spilled",len(sp),"skipped",[s.skipped_reason for s in r.skipped][:2], "spilled best rank idx",min([r.ranked.index(x) for x in sp],default=None))
# slots inside window bounds + after now
bad=0
for hh in range(0,24):
  for mm in (0,29,59):
    now=datetime(2026,11,25,hh,mm,tzinfo=timezone.utc); r=run(accts[:30]+[replace(a,timezone="Australia/Perth") for a in accts[30:]],{},P,now=now)
    for x in r.ranked:
        t=x.local_call_time; ws=[w for d in (0,1,2,3,4,5) for w in window_bounds(cfg,x.account.timezone,t.date()) ]
        if not any(w.start<=t<w.end for w in ws) or t<now: bad+=1
print("slots outside windows or before now across 72 run times:",bad)

# same run, Perth/Sydney/Auckland
for lbl,now in [("04:00 UTC",datetime(2026,11,25,4,0,tzinfo=timezone.utc)),("23:00 UTC",datetime(2026,11,25,23,0,tzinfo=timezone.utc))]:
    mix=[acct("S",tz="Australia/Sydney"),acct("P",tz="Australia/Perth"),acct("A",tz="Pacific/Auckland")]
    r=run(mix,{},[pr(i) for i in "SPA"],now=now); print(lbl,[(x.account.timezone.split('/')[1],x.window,x.local_call_time.strftime('%a %d %H:%M %z')) for x in r.ranked])
# DST: run Fri 2 Oct 2026 16:00 Sydney with 120 accounts (spill across DST Sunday), Auckland run Fri 2 Apr 2027
many=[acct(f"M{i}",name=f"M{i:03d}") for i in range(130)]
cfg2=replace(cfg,limits=replace(cfg.limits,daily_call_cap=200))
now=datetime(2026,10,2,13,0,tzinfo=ZoneInfo("Australia/Sydney")).astimezone(timezone.utc)
r=rank_accounts(many,{},[pr(f"M{i}",name=f"m{i}") for i in range(130)],{},set(),cfg2,now)
d=Counter(x.local_call_time.strftime("%a %d %b %z") for x in r.ranked); print("DST spill Fri 2 Oct 13:00 -> ",dict(d),"min gap",gaps([x.local_call_time for x in r.ranked]))
now=datetime(2027,4,2,13,0,tzinfo=ZoneInfo("Pacific/Auckland")).astimezone(timezone.utc)
r=rank_accounts([replace(a,timezone="Pacific/Auckland") for a in many],{},[pr(f"M{i}",name=f"m{i}") for i in range(130)],{},set(),cfg2,now)
print("DST Auckland",dict(Counter(x.local_call_time.strftime("%a %d %b %z") for x in r.ranked)))
# capacity: absolute max a day holds and cap beyond
r=rank_accounts(many,{},[pr(f"M{i}",name=f"m{i}") for i in range(130)],{},set(),cfg2,datetime(2026,11,25,20,0,tzinfo=timezone.utc))
print("130 on one full day:",dict(Counter(x.local_call_time.strftime("%a %d") for x in r.ranked)),"gap",gaps([x.local_call_time for x in r.ranked]))
print("== cooldown vs spill edge: now 16:29 Syd, cooldown_until = next day")
now=datetime(2026,11,25,16,29,tzinfo=ZoneInfo("Australia/Sydney")).astimezone(timezone.utc)
x=run([acct("E")],{},[pr("E")],{"F-E":date(2026,11,26)},set(),now).ranked[0]; print(x.local_call_time,x.score_breakdown["cooldown_penalty"],x.reasons[-2:])
print("== guard: no state/accounts/signals imports"); import re as _re; print(_re.findall(r"^(?:from|import) src\.\w+",open("src/ranker.py").read(),_re.M))
print("== Prospect with None title/blank name"); 
try: print(len(run([acct("Q")],{},[Prospect(nooks_id="x",sf_contact_id=None,sf_account_id="F-Q",nooks_account_id=None,name="",title=None,phone_e164="+61400000000")]).needs_prospect))
except Exception as e: print(repr(e))
