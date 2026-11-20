"""Независимый точный эталон для небольших контрольных каталогов.
Это не backend продукта: полный перебор служит только проверкой постановки ТЗ.
Все суммы в копейках. Нет сети, ML, БД, заказов и случайных чисел.
"""
from __future__ import annotations
import itertools,json,math,hashlib,copy
from datetime import date,timedelta,datetime,time,timezone
from zoneinfo import ZoneInfo
SLOTS=("dinner_plate","dessert_plate","drinking_glass","cutlery_set","napkin","tablecloth")
REQUIRED={"dinner_plate","drinking_glass","cutlery_set","napkin"}
ALGORITHM_VERSION="spec-oracle-1.0.0"
def canonical(value):
 return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False).encode()
def digest(value):return hashlib.sha256(canonical(value)).hexdigest()
def fail(code,path=""):
 return {"outcome":"VALIDATION_ERROR","error_code":code,"field":path}
def effective_expiry(now_iso,ttl_seconds,needed_by,lead_days):
 now=datetime.fromisoformat(now_iso.replace("Z","+00:00"));result=now+timedelta(seconds=ttl_seconds)
 if needed_by:
  latest=date.fromisoformat(needed_by)-timedelta(days=lead_days)
  cap=datetime.combine(latest+timedelta(days=1),time.min,ZoneInfo("Europe/Moscow")).astimezone(timezone.utc)
  result=min(result,cap)
 return result.isoformat(timespec="milliseconds").replace("+00:00","Z")
def solve(snapshot,request):
 byid={x["sku_id"]:x for x in snapshot["catalog"]}
 if len(byid)!=len(snapshot["catalog"]):raise ValueError("invalid fixture: duplicate sku")
 t={x["slot_id"]:x for x in snapshot["template_slots"]}
 req=request
 def isint(x):return isinstance(x,int)and not isinstance(x,bool)
 if not isint(req["guest_count"])or not 1<=req["guest_count"]<=12:return fail("VALIDATION_ERROR","guest_count")
 if not isint(req["budget_minor"])or not 50000<=req["budget_minor"]<=5000000:return fail("VALIDATION_ERROR","budget_minor")
 if req["currency"]!="RUB":return fail("VALIDATION_ERROR","currency")
 if len(req["slots"])!=6 or {x["slot_id"]for x in req["slots"]}!=set(SLOTS):return fail("VALIDATION_ERROR","slots")
 inputs={x["slot_id"]:x for x in req["slots"]}
 for slot in SLOTS:
  x=inputs[slot]
  if not isint(x["owned_units"])or not 0<=x["owned_units"]<=99:return fail("VALIDATION_ERROR","owned_units")
  if slot in REQUIRED and not x["enabled"]:return fail("VALIDATION_ERROR",slot+".enabled")
  if x["selection_mode"]not in("EXACT_ONLY","APPROVED_ALTERNATIVES"):return fail("VALIDATION_ERROR","selection_mode")
  if x["enabled"]and x["allowed_colors"]and x["preferred_color"]is not None and x["preferred_color"]not in x["allowed_colors"]:return fail("CONTRADICTORY_PREFERENCES",slot)
 today=date.fromisoformat(snapshot["business_date"])
 needed_by=date.fromisoformat(req["needed_by"])if req["needed_by"]else None
 if needed_by and not today<=needed_by<=today+timedelta(days=30):return fail("VALIDATION_ERROR","needed_by")
 remaining={}
 owned={}
 for slot in SLOTS:
  n=(1 if slot=="tablecloth"else req["guest_count"])if inputs[slot]["enabled"]else 0
  owned[slot]=min(inputs[slot]["owned_units"],n)
  remaining[slot]=n-owned[slot]
 if not any(remaining.values()):
  return {"outcome":"NO_PURCHASE_REQUIRED","lines":[],"subtotal_minor":0,"shipping_minor":0,"total_minor":0,"objective":None,"earliest_date":None,"minimum_total_minor":0}
 f=req["fulfillment"]
 if f["mode"]=="PICKUP":
  if f["zone"]is not None:return fail("VALIDATION_ERROR","fulfillment.zone")
  fee,transit=0,0
 elif f["mode"]=="LOCAL_DELIVERY":
  if f["zone"]not in("CENTER","NORTH"):return fail("UNSUPPORTED_ZONE","fulfillment.zone")
  fee,transit=30000,1
 else:return fail("VALIDATION_ERROR","fulfillment.mode")
 dim=req["table_dimensions"]
 if remaining["tablecloth"]:
  if not dim:return fail("VALIDATION_ERROR","table_dimensions")
  if not 60<=dim["length_cm"]<=300 or not 40<=dim["width_cm"]<=150:return fail("VALIDATION_ERROR","table_dimensions")
 options=[]
 for slot in SLOTS:
  n=remaining[slot];i=inputs[slot];m=t[slot]
  if n==0:options.append([None]);continue
  allowed={m["primary_sku_id"]}
  if i["selection_mode"]=="APPROVED_ALTERNATIVES":allowed.update(m["alternative_sku_ids"])
  cand=[]
  for sku_id in sorted(allowed):
   p=byid.get(sku_id)
   if not p or not p["active"]or p["slot_id"]!=slot:continue
   packs=(n+p["pack_units"]-1)//p["pack_units"]
   if packs>p["stock_on_hand_packs"]-p["reserved_packs"]:continue
   if i["allowed_colors"]and p["color"]not in i["allowed_colors"]:continue
   if slot=="tablecloth":
    a,b=p["length_cm"],p["width_cm"];x,y=dim["length_cm"],dim["width_cm"]
    if a is None or b is None:continue
    if not((x+30<=a<=x+60 and y+30<=b<=y+60)or(x+30<=b<=x+60 and y+30<=a<=y+60)):continue
   if needed_by and today+timedelta(days=p["handling_days"]+transit)>needed_by:continue
   units=packs*p["pack_units"]
   cand.append({"slot_id":slot,"sku_id":sku_id,"pack_count":packs,"pack_units":p["pack_units"],"delivered_units":units,"required_units":n+owned[slot],"effective_owned_units":owned[slot],"excess_units":units-n,"unit_pack_price_minor":p["price_minor"],"line_total_minor":packs*p["price_minor"],"substitution":sku_id!=m["primary_sku_id"],"soft_color_mismatch":i["preferred_color"]is not None and p["color"]!=i["preferred_color"],"handling_days":p["handling_days"]})
  if not cand:return {"outcome":"INFEASIBLE","reason_code":"NO_CANDIDATE","slot_id":slot,"minimum_total_minor":None}
  options.append(cand)
 count=math.prod(len(x)for x in options)
 if count>200000:raise ValueError("Oracle limit: not a production solver")
 best=None;best_lines=None;minimum=None
 for choice in itertools.product(*options):
  lines=[x for x in choice if x]
  total=sum(x["line_total_minor"]for x in lines)+fee
  minimum=total if minimum is None else min(minimum,total)
  if total>req["budget_minor"]:continue
  objective=(sum(x["substitution"]for x in lines),sum(x["soft_color_mismatch"]for x in lines),sum(x["excess_units"]for x in lines),total,tuple(x["sku_id"]if x else ""for x in choice))
  if best is None or objective<best:best,best_lines=objective,lines
 if best is None:return {"outcome":"INFEASIBLE","reason_code":"BUDGET_TOO_LOW","slot_id":None,"minimum_total_minor":minimum}
 return {"outcome":"QUOTED","lines":best_lines,"subtotal_minor":best[3]-fee,"shipping_minor":fee,"total_minor":best[3],"objective":{"substituted_slots":best[0],"soft_color_mismatches":best[1],"excess_units":best[2],"total_minor":best[3],"sku_tuple":list(best[4])},"earliest_date":(today+timedelta(days=max(x["handling_days"]for x in best_lines)+transit)).isoformat(),"minimum_total_minor":minimum}
def verify_solution(snapshot,request,result):
 """Independent feasibility/metadata verifier. Exact optimum is checked separately."""
 if not isinstance(result,dict) or result.get("outcome")not in {"QUOTED","INFEASIBLE","NO_PURCHASE_REQUIRED","VALIDATION_ERROR"}:return ["MALFORMED_RESULT"]
 if result["outcome"]!="QUOTED":return []
 errors=[];products={x["sku_id"]:x for x in snapshot["catalog"]};requests={x["slot_id"]:x for x in request["slots"]};templates={x["slot_id"]:x for x in snapshot["template_slots"]}
 if not isinstance(result.get("lines"),list)or not result["lines"]:return ["EMPTY_QUOTE"]
 seen=set();subtotal=0;chosen={};handling=[]
 for line in result["lines"]:
  if not isinstance(line,dict):errors.append("MALFORMED_LINE");continue
  s=line.get("slot_id")
  if s not in requests:errors.append("UNKNOWN_SLOT");continue
  if s in seen:errors.append("DUPLICATE_SLOT")
  seen.add(s)
  p=products.get(line.get("sku_id"));q=requests[s];t=templates[s]
  if p is None:errors.append("UNKNOWN_SKU");continue
  if not p["active"]or p["slot_id"]!=s:errors.append("INVALID_SKU")
  allowed=[t["primary_sku_id"]]+(t["alternative_sku_ids"]if q["selection_mode"]=="APPROVED_ALTERNATIVES"else [])
  if p["sku_id"]not in allowed:errors.append("UNAPPROVED_SKU")
  required=(1 if s=="tablecloth"else request["guest_count"])if q["enabled"]else 0
  effective_owned=min(q["owned_units"],required)
  remaining=required-effective_owned
  count=(remaining+p["pack_units"]-1)//p["pack_units"]
  expected={"pack_count":count,"pack_units":p["pack_units"],"delivered_units":count*p["pack_units"],"required_units":required,"effective_owned_units":effective_owned,"excess_units":count*p["pack_units"]-remaining,"unit_pack_price_minor":p["price_minor"],"line_total_minor":count*p["price_minor"],"handling_days":p["handling_days"]}
  for field,val in expected.items():
   if type(line.get(field))is not int or line[field]!=val:errors.append("FIELD_"+field.upper())
  if count<=0:errors.append("UNNEEDED_LINE")
  if count>p["stock_on_hand_packs"]-p["reserved_packs"]:errors.append("STOCK")
  if q["allowed_colors"]and p["color"]not in q["allowed_colors"]:errors.append("COLOR")
  substitution=p["sku_id"]!=t["primary_sku_id"]
  mismatch=q["preferred_color"]is not None and p["color"]!=q["preferred_color"]
  if type(line.get("substitution"))is not bool or line["substitution"]!=substitution:errors.append("SUBSTITUTION")
  if type(line.get("soft_color_mismatch"))is not bool or line["soft_color_mismatch"]!=mismatch:errors.append("SOFT_COLOR")
  subtotal+=expected["line_total_minor"];handling.append(p["handling_days"])
  chosen[s]={"sku_id":p["sku_id"],"substitution":substitution,"soft_color_mismatch":mismatch,"excess_units":expected["excess_units"]}
  if s=="tablecloth":
   d=request["table_dimensions"];a,b=p["length_cm"],p["width_cm"]
   if not d or a is None or b is None:errors.append("DIMENSIONS")
   else:
    x,y=d["length_cm"],d["width_cm"]
    if not((30<=a-x<=60 and 30<=b-y<=60)or(30<=b-x<=60 and 30<=a-y<=60)):errors.append("DIMENSIONS")
 for s,q in requests.items():
  n=(1 if s=="tablecloth"else request["guest_count"])if q["enabled"]else 0
  if n>q["owned_units"]and s not in seen:errors.append("MISSING_SLOT")
 if [x.get("slot_id")for x in result["lines"]if isinstance(x,dict)]!=[s for s in SLOTS if s in seen]:errors.append("LINE_ORDER")
 fee=0 if request["fulfillment"]["mode"]=="PICKUP"else 30000
 if result.get("subtotal_minor")!=subtotal or result.get("shipping_minor")!=fee or result.get("total_minor")!=subtotal+fee:errors.append("TOTAL")
 if subtotal+fee>request["budget_minor"]:errors.append("BUDGET")
 expected_obj={"substituted_slots":sum(x["substitution"]for x in chosen.values()),"soft_color_mismatches":sum(x["soft_color_mismatch"]for x in chosen.values()),"excess_units":sum(x["excess_units"]for x in chosen.values()),"total_minor":subtotal+fee,"sku_tuple":[chosen[s]["sku_id"]if s in chosen else ""for s in SLOTS]}
 if result.get("objective")!=expected_obj:errors.append("OBJECTIVE")
 if handling:
  transit=0 if request["fulfillment"]["mode"]=="PICKUP"else 1
  earliest=date.fromisoformat(snapshot["business_date"])+timedelta(days=max(handling)+transit)
  if result.get("earliest_date")!=earliest.isoformat():errors.append("EARLIEST_DATE")
  if request["needed_by"]and earliest>date.fromisoformat(request["needed_by"]):errors.append("DEADLINE")
 return sorted(set(errors))

