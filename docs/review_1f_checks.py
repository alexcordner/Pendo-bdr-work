"""Reviewer scratch checks for task 1f (offline, synthetic data). Run: python docs/review_1f_checks.py
Not part of the test suite; prints observations quoted in docs/review_1f.md."""
import sys; sys.path.insert(0,'.')
from dataclasses import replace
from collections import Counter
import random, json, hashlib
from datetime import *
from zoneinfo import ZoneInfo
from src.config import load_config, tier_for, next_working_windows
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
a=acct(emp=100,is_sdr_target=True); print("S1",score_account(a,[sig(s=.8,days=9)],True,False,False,cfg,NOW))
# S2: no signal, tier1, no prospect, cooldown, flagged
a=acct(emp=2000,non_anz_billing_anz_hq=True); print("S2",score_account(a,[],False,True,False,cfg,NOW))
# S3: 3 signals, tier1, prospect, sdr -> clamp
a=acct(emp=200,is_sdr_target=True); print("S3",score_account(a,[sig(s=.95,days=0),sig(k=SignalKind.MA,s=.9,days=0),sig(k=SignalKind.NEW_HIRE,s=.5,days=3)],True,False,False,cfg,NOW))
print("== tiers")
for e in [200,199,50,49,20,19,2000,2001,0,-5,None]:
    try: print(e,tier_for(cfg,e), score_account(acct(emp=e),[],False,False,False,cfg,NOW)[1]["icp_fit"])
    except Exception as ex: print(e,"EXC",repr(ex))
print("== recency")
for d in [0,1,22,45,46]: print(d,score_account(acct(),[sig(days=d)],False,False,False,cfg,NOW)[1]["recency"])
print("future", score_account(acct(),[sig(days=-10)],False,False,False,cfg,NOW)[1]["recency"], "nodate",score_account(acct(),[sig(date_=False)],False,False,False,cfg,NOW)[1]["recency"])
# naive datetime source_date?
try:
    s=Signal("F-A1",SignalKind.FUNDING,.5,"n",datetime(2026,11,20),"e"); print(score_account(acct(),[s],False,False,False,cfg,NOW))
except Exception as ex: print("naive source_date EXC",repr(ex))
print("== personas")
titles=["Chief Technology Officer","CTO & Co-founder","Head of Product & Design","VP, Product","Product Manager (Growth)","Digital Transformation Manager","Senior Product Manager","Customer Success Director","Head of CX","Group Head of Digital Workplace","",None,"Director","Director of Product Management","Chief Product Officer","VP Product","VP of Product","Vice President, Product","Vice President of Product","Head of Product, ANZ","Head of Products","Product Managers","Chief Product & Technology Officer","Head of Customer Experience","Head of Customer Success ANZ","Head Of Product","Director, Product","Director of Product Management","Product Owner","Head of Digital Product","Chief Digital & Information Officer","Chief Information Officer","Head of Engineering","Engineering Manager","Director of Engineering","Chief Executive Officer","Head of Customer Support","Head of Digital Transformation","Group Product Manager","Principal Product Manager","Director of Customer Experience","VP Customer Success","GM Product","Chief Digital Officer","Head of Digital Experience","CTO","Co-CTO","Product Director","Digital Director","Head of Technology","Group CTO","Head of IT Operations","Head of Information Technology","Product Lead","Director of Product-Led Growth"]
for t in titles: print(repr(t), _persona_rank(t,cfg))

def run(accts,sigs={},ps=(),cool={},last=set(),now=NOW): return rank_accounts(accts,sigs,list(ps),cool,last,cfg,now)
def dump(r): return json.dumps([(x.account.sf_account_id,x.score,x.window,str(x.local_call_time),x.reasons) for x in r.ranked]+[(x.account.sf_account_id,x.skipped_reason) for x in r.skipped],default=str)
# determinism: 120 accounts mixed tz, ties
rnd=random.Random(1)
tzs=["Australia/Sydney","Australia/Perth","Pacific/Auckland","Australia/Brisbane",None]
accts=[acct(str(i),emp=rnd.choice([10,30,100,500,None]),tz=rnd.choice(tzs),timezone_guessed=rnd.random()<.3,is_sdr_target=rnd.random()<.2) for i in range(120)]
sigs={f"F-{i}":[sig(str(i),s=rnd.choice([.5,.8]),days=rnd.choice([1,5,5,30]))] for i in range(0,120,2)}
ps=[pr(str(i),name=f"N{i}") for i in range(0,120,3)]
base=dump(run(accts,sigs,ps))
ok=True
for seed in range(20):
    r2=random.Random(seed); a=accts[:]; p=ps[:]; r2.shuffle(a); r2.shuffle(p)
    s2={k:random.Random(seed).sample(v,len(v)) for k,v in sigs.items()}
    ok&=dump(run(a,s2,p))==base
print("shuffle determinism",ok, "ranked",len(run(accts,sigs,ps).ranked))
# equal-score ties w/ identical names?
t=[acct("X1",name="Same"),acct("X2",name="Same")]
print("tie order",[x.account.sf_account_id for x in run(t).ranked],[x.account.sf_account_id for x in run(t[::-1]).ranked])
print("== item4: no prospect")
r=run([acct("N1")],{"F-N1":[sig("N1")]},[])
x=r.ranked[0]; print(x.score,x.reasons, r.prospects)
print("== cooldown/today")
# now=23:00 UTC Wed = Thu 10:00 Sydney. cooldown_until=Thu 2026-11-26 (Sydney 'today')
now=datetime(2026,11,25,23,0,tzinfo=timezone.utc)
print("UTC today",now.date(),"; Sydney",now.astimezone(ZoneInfo("Australia/Sydney")).date())
a=acct("C1"); 
for cu in [date(2026,11,25),date(2026,11,26),date(2026,11,27)]:
    r=run([a],{},[],{"F-C1":cu},set(),now); print("cooldown_until",cu,"penalty",r.ranked[0].score_breakdown["cooldown_penalty"])
# now=01:00 UTC Thu = Thu 12:00 Sydney; 05:00 Wed Perth? also Auckland
now=datetime(2026,11,25,13,30,tzinfo=timezone.utc) # Thu 00:30 Sydney
print("UTC",now.date(),"Syd",now.astimezone(ZoneInfo("Australia/Sydney")).date(),"LA?")
print("== windows")
def wins(tz,now): 
    w=next_working_windows(cfg,tz,now); return [(x.name,str(x.start),str(x.end)) for x in w]
N=datetime(2026,11,25,20,0,tzinfo=timezone.utc)
for tz in ["Australia/Sydney","Australia/Perth","Pacific/Auckland","Australia/Brisbane","Australia/Adelaide"]:
    print(tz,N.astimezone(ZoneInfo(tz)).strftime("%a %H:%M"),wins(tz,N)[:1])
# Friday evening -> Monday
F=datetime(2026,11,27,8,30,tzinfo=timezone.utc)  # Fri 19:30 Sydney
print("Fri eve",wins("Australia/Sydney",F))
# DST: Sydney DST start Sun 2026-10-04; end Sun 2027-04-04. Auckland DST start 2026-09-27; end 2027-04-04
for n,tz in [(datetime(2026,10,2,9,0,tzinfo=timezone.utc),"Australia/Sydney"),(datetime(2027,4,2,9,0,tzinfo=timezone.utc),"Australia/Sydney"),(datetime(2027,4,2,9,0,tzinfo=timezone.utc),"Pacific/Auckland")]:
    w=next_working_windows(cfg,tz,n); print("DST",tz,n,[(x.name,x.start.isoformat()) for x in w][:1])
# now exactly at window start / end
for t in [datetime(2026,11,25,8,45,tzinfo=ZoneInfo("Australia/Sydney")),datetime(2026,11,25,11,30,tzinfo=ZoneInfo("Australia/Sydney")),datetime(2026,11,25,11,29,59,tzinfo=ZoneInfo("Australia/Sydney")),datetime(2026,11,25,16,30,tzinfo=ZoneInfo("Australia/Sydney"))]:
    r=run([acct("W")],{},[],{},set(),t.astimezone(timezone.utc)); x=r.ranked[0]; print("now",t.strftime("%H:%M:%S"),x.window,x.local_call_time)
# 40 accounts same window
accts=[acct(str(i),name=f"A{i:02d}") for i in range(40)]
for gv in (False,True):
    ac=[replace(a,timezone_guessed=gv) for a in accts]
    r=run(ac); ts=[x.local_call_time for x in r.ranked]
    mins={(t.hour,t.minute) for t in ts}; print("guessed",gv,len(ts),"distinct minutes",len(mins),min(ts).time(),max(ts).time(),"dups",len(ts)-len(set(ts)))
# now inside window, guessed, many
nw=datetime(2026,11,25,10,50,tzinfo=ZoneInfo("Australia/Sydney")).astimezone(timezone.utc)
ac=[replace(a,timezone_guessed=True) for a in accts]; r=run(ac,now=nw); ts=[x.local_call_time for x in r.ranked]
print("inwindow guessed 40: windows",{x.window for x in r.ranked},min(ts).time(),max(ts).time(),"distinct min",len({(t.hour,t.minute) for t in ts}))
# now 10:59 -> 31 minutes left, 40 accounts
nw=datetime(2026,11,25,11,29,tzinfo=ZoneInfo("Australia/Sydney")).astimezone(timezone.utc)
r=run(accts,now=nw); ts=[x.local_call_time for x in r.ranked]; print("1 min left, 40 accts:",min(ts).time(),max(ts).time(),"distinct",len(set(ts)))
# cross-tz: Perth+Sydney simultaneously
mix=[acct("S1",tz="Australia/Sydney"),acct("P1",tz="Australia/Perth"),acct("A1",tz="Pacific/Auckland")]
r=run(mix); print([(x.account.timezone,x.window,x.local_call_time.isoformat()) for x in r.ranked])
# tz-aware time ties across tz: ranked order unaffected by tz
# summarise
print(summarise(r))
# cap after skips
cap=cfg.limits.daily_call_cap
ac=[acct(str(i),name=f"B{i:03d}",tz=None if i<10 else "Australia/Sydney") for i in range(60)]
r=run(ac); print("cap",len(r.ranked),len(r.skipped),summarise(r).splitlines()[:5])
# guessed-tz + invalid tz string
try: run([acct("Z",tz="Mars/Base")])
except Exception as e: print("bad tz",repr(e))
# empty tz string
try: print(run([acct("Z",tz="")]).skipped[0].skipped_reason)
except Exception as e: print("empty tz",repr(e))
import random
# hashseed determinism digest
rnd=random.Random(5)
accts=[acct(str(i),emp=rnd.choice([10,30,100,500,None]),tz=rnd.choice(["Australia/Sydney","Australia/Perth","Pacific/Auckland"]),timezone_guessed=rnd.random()<.3,is_sdr_target=rnd.random()<.2) for i in range(150)]
sigs={f"F-{i}":[sig(str(i),s=rnd.choice([.5,.8]),days=rnd.choice([1,5,30]))] for i in range(0,150,2)}
ps=[pr(str(i),name=f"N{i}") for i in range(0,150,3)]
print("DIGEST",hashlib.md5(dump(run(accts,sigs,ps)).encode()).hexdigest())
print("== displacement")
nopros=[acct(f"NP{i}",name=f"NP{i:02d}") for i in range(45)]
callable_=[acct(f"C{i}",name=f"C{i:02d}",emp=30) for i in range(10)]
sg={f"F-NP{i}":[sig(f"NP{i}")] for i in range(45)}
r=run(nopros+callable_,sg,[pr(f"C{i}",name=f"c{i}") for i in range(10)])
print("callable ranked:",sum(1 for x in r.ranked if r.prospects[account_key(x.account)]),"of 10; no-prospect ranked:",sum(1 for x in r.ranked if not r.prospects[account_key(x.account)]))
print("== last sheet / cooldown not exclusion")
top=acct("T",name="Top"); oth=[acct(f"O{i}",emp=30,name=f"O{i}") for i in range(3)]
r=run([top]+oth,{"F-T":[sig("T")]},[pr("T")]+[pr(f"O{i}",name=f"o{i}") for i in range(3)],{},{"F-T"})
print([(x.account.name,x.score) for x in r.ranked])
r=run([top]+oth,{"F-T":[sig("T")]},[pr("T")],{"F-T":date(2026,12,30)},set()); print("cooldown 5wks:",[(x.account.name,x.score) for x in r.ranked][:2])
print("== cooldown datetime value")
try: run([acct("D")],{},[],{"F-D":datetime(2026,11,30,tzinfo=timezone.utc)})
except Exception as e: print("EXC",repr(e))
print("== evening run (Tue 20:00 AEDT), cooldown_until = call date Wed")
now=datetime(2026,11,24,20,0,tzinfo=ZoneInfo("Australia/Sydney")).astimezone(timezone.utc)
x=run([acct("E")],{},[pr("E")],{"F-E":date(2026,11,25)},set(),now).ranked[0]; print(now, x.local_call_time.date(), x.score_breakdown["cooldown_penalty"], x.reasons[-1])
print("== cross-group collisions Sydney 20 confirmed + 20 guessed")
ac=[acct(f"G{i}",name=f"G{i:02d}",timezone_guessed=(i%2==0)) for i in range(40)]
r=run(ac); from collections import Counter
c=Counter((x.local_call_time.hour,x.local_call_time.minute) for x in r.ranked); print("max accounts/minute",max(c.values()),"minutes used",len(c))
print("== pick_prospect with titles")
P=[pr("Q",title="Director of Product",name="a",do_not_call=True),pr("Q",title="Product Manager",name="b"),pr("Q",title="Head of Product",name="c",phone=None),pr("Q",title="Head of Digital",name="d",opted_out_email=True)]
print([p.name for p in pick_prospects(P,cfg)], cfg.limits.max_per_account_per_run)
print("== blocked + no-phone mix not skipped")
r=run([acct("Q")],{},P); print(len(r.ranked),len(r.skipped))
r=run([acct("Q")],{},[P[0],P[3]]); print("all blocked ->",[s.skipped_reason for s in r.skipped])
print("== multi-prospect shared across sf/nooks id dedup")
p=Prospect(nooks_id="PX",sf_contact_id=None,sf_account_id="F-Z",nooks_account_id="N-Z",name="Z",title="CTO",phone_e164="+61400000009")
a=acct("Z",nooks_account_id="N-Z"); r=run([a],{},[p]); print(len(r.prospects["F-Z"]))
print("== now naive")
try: run([acct("Z")],now=datetime(2026,11,25))
except Exception as e: print(repr(e))