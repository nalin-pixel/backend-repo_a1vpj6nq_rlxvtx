import os
from datetime import datetime, timezone
from typing import Dict, Any

from fastapi import FastAPI, APIRouter, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from database import db, create_document, get_documents
from schemas import UE, PDUSession, PolicyRule, Slice, NFService, LogEntry, HealthStatus

app = FastAPI(title="5G Core Simulation")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Utility logging function
async def log(nf: str, level: str, message: str, context: Dict[str, Any] | None = None):
    entry = LogEntry(nf=nf, level=level, message=message, context=context or {})
    create_document("logentry", entry)


# Routers per NF (simulating independent microservices)
amf = APIRouter(prefix="/amf", tags=["AMF"])
smf = APIRouter(prefix="/smf", tags=["SMF"])
upf = APIRouter(prefix="/upf", tags=["UPF"])
nrf = APIRouter(prefix="/nrf", tags=["NRF"])
nssf = APIRouter(prefix="/nssf", tags=["NSSF"])
pcf = APIRouter(prefix="/pcf", tags=["PCF"])
udm = APIRouter(prefix="/udm", tags=["UDM/AUSF"])


@app.get("/health")
def root_health():
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}


@app.get("/metrics")
def metrics():
    # simple counters from DB sizes
    return {
        "ues": db["ue"].count_documents({}) if db else 0,
        "sessions": db["pdusession"].count_documents({}) if db else 0,
        "logs": db["logentry"].count_documents({}) if db else 0,
    }


@app.get("/logs/stream")
def stream_logs():
    def gen():
        last_count = 0
        while True:
            try:
                count = db["logentry"].count_documents({}) if db else 0
                if count > last_count:
                    # fetch the latest one
                    docs = db["logentry"].find({}).sort("created_at", -1).limit(1)
                    for d in docs:
                        payload = {
                            "nf": d.get("nf"),
                            "level": d.get("level"),
                            "message": d.get("message"),
                            "context": d.get("context", {}),
                            "ts": d.get("created_at").isoformat() if d.get("created_at") else None,
                        }
                        yield f"data: {payload}\n\n"
                    last_count = count
            except Exception:
                pass
            import time
            time.sleep(1)

    return StreamingResponse(gen(), media_type="text/event-stream")


# ---------------------- NRF ----------------------
@nrf.post("/register")
def nrf_register(service: NFService):
    existing = db["nfservice"].find_one({"nf_id": service.nf_id})
    if existing:
        db["nfservice"].update_one({"nf_id": service.nf_id}, {"$set": service.model_dump()})
        msg = "updated"
    else:
        create_document("nfservice", service)
        msg = "registered"
    return {"status": msg}


@nrf.get("/services")
def nrf_services():
    return get_documents("nfservice", {})


@nrf.get("/health")
def nrf_health():
    return HealthStatus(nf="NRF", status="HEALTHY")


# ---------------------- NSSF ----------------------
@nssf.post("/select-slice")
def select_slice(payload: Dict[str, Any]):
    supi = payload.get("supi")
    plmn = payload.get("plmn")
    # naive: pick first slice matching PLMN
    sl = db["slice"].find_one({"plmns": plmn}) or db["slice"].find_one({})
    if not sl:
        raise HTTPException(404, "No slices configured")
    slice_id = sl.get("slice_id")
    # record selection
    create_document("logentry", LogEntry(nf="NSSF", level="INFO", message="Slice selected", context={"supi": supi, "slice": slice_id}))
    return {"slice_id": slice_id}


@nssf.get("/health")
def nssf_health():
    return HealthStatus(nf="NSSF", status="HEALTHY")


# ---------------------- UDM/AUSF ----------------------
@udm.post("/authenticate")
def authenticate(payload: Dict[str, Any]):
    supi = payload.get("supi")
    ue = db["ue"].find_one({"supi": supi})
    if not ue:
        raise HTTPException(404, "UE not found")
    token = f"auth-{supi}"
    create_document("logentry", LogEntry(nf="UDM/AUSF", level="INFO", message="UE authenticated", context={"supi": supi}))
    return {"result": "OK", "token": token}


@udm.get("/health")
def udm_health():
    return HealthStatus(nf="UDM/AUSF", status="HEALTHY")


# ---------------------- PCF ----------------------
@pcf.post("/policy")
def set_policy(rule: PolicyRule):
    existing = db["policyrule"].find_one({"policy_id": rule.policy_id})
    if existing:
        db["policyrule"].update_one({"policy_id": rule.policy_id}, {"$set": rule.model_dump()})
        action = "updated"
    else:
        create_document("policyrule", rule)
        action = "created"
    create_document("logentry", LogEntry(nf="PCF", level="INFO", message=f"Policy {action}", context={"policy_id": rule.policy_id}))
    return {"status": action}


@pcf.get("/policy/{policy_id}")
def get_policy(policy_id: str):
    rule = db["policyrule"].find_one({"policy_id": policy_id})
    if not rule:
        raise HTTPException(404, "Policy not found")
    return rule


@pcf.get("/health")
def pcf_health():
    return HealthStatus(nf="PCF", status="HEALTHY")


# ---------------------- AMF ----------------------
@amf.post("/register-ue")
def amf_register_ue(ue: UE):
    existing = db["ue"].find_one({"supi": ue.supi})
    if existing:
        db["ue"].update_one({"supi": ue.supi}, {"$set": ue.model_dump() | {"registered": True, "last_seen": datetime.now(timezone.utc)}})
        msg = "updated"
    else:
        ue.registered = True
        ue.last_seen = datetime.now(timezone.utc)
        create_document("ue", ue)
        msg = "registered"
    create_document("logentry", LogEntry(nf="AMF", level="INFO", message=f"UE {msg}", context={"supi": ue.supi}))
    return {"status": msg}


@amf.post("/ue-registration-flow")
def ue_registration_flow(payload: Dict[str, Any]):
    """Simulate UE Registration across UDM/AUSF and NSSF.
    Steps: AMF receives NAS, queries UDM for auth, calls NSSF for slice, marks UE registered.
    """
    supi = payload.get("supi")
    plmn = payload.get("plmn")
    if not supi or not plmn:
        raise HTTPException(400, "supi and plmn required")

    # ensure UE exists (or create)
    ue = db["ue"].find_one({"supi": supi})
    if not ue:
        ue_obj = UE(supi=supi, plmn=plmn, registered=False)
        create_document("ue", ue_obj)

    # authenticate
    auth = authenticate({"supi": supi})
    if auth.get("result") != "OK":
        raise HTTPException(401, "Authentication failed")

    # slice selection
    sl = select_slice({"supi": supi, "plmn": plmn})

    # update UE registration
    db["ue"].update_one({"supi": supi}, {"$set": {"registered": True, "last_seen": datetime.now(timezone.utc), "slices": [sl["slice_id"]], "amf_id": "amf-1"}})
    create_document("logentry", LogEntry(nf="AMF", level="INFO", message="UE registration flow complete", context={"supi": supi, "slice": sl["slice_id"]}))
    return {"result": "OK", "slice": sl["slice_id"]}


@amf.get("/health")
def amf_health():
    return HealthStatus(nf="AMF", status="HEALTHY")


# ---------------------- SMF ----------------------
@smf.post("/pdu-session")
def create_pdu_session(session: PDUSession):
    # attach policy
    policy = db["policyrule"].find_one({})
    session.qos_rules = policy.get("qos") if policy else {"5qi": 9}
    create_document("pdusession", session)
    create_document("logentry", LogEntry(nf="SMF", level="INFO", message="PDU session created", context={"session_id": session.session_id, "supi": session.supi}))
    return {"status": "created"}


@smf.post("/establish-session")
def establish_session(payload: Dict[str, Any]):
    """Simulate PDU Session Establishment across PCF and UPF.
    Steps: SMF gets policy from PCF, selects UPF, installs rules via N4, returns session info.
    """
    supi = payload.get("supi")
    dnn = payload.get("dnn", "internet")
    s_nssai = payload.get("slice")
    if not supi:
        raise HTTPException(400, "supi required")

    ue = db["ue"].find_one({"supi": supi, "registered": True})
    if not ue:
        raise HTTPException(400, "UE not registered")

    # get a policy
    pol = db["policyrule"].find_one({})
    qos = pol.get("qos") if pol else {"5qi": 9}

    # select UPF (first available)
    upf_svc = db["nfservice"].find_one({"nf_type": "UPF"})
    upf_id = upf_svc.get("nf_id") if upf_svc else "upf-1"

    # create session
    sess = PDUSession(session_id=f"sess-{supi}-{int(datetime.now().timestamp())}", supi=supi, dnn=dnn, s_nssai=s_nssai or (ue.get("slices") or ["default"])[0], smf_id="smf-1", upf_id=upf_id, qos_rules=qos)
    create_document("pdusession", sess)

    # install to UPF
    db["upfstate"].update_one({"upf_id": upf_id}, {"$setOnInsert": {"upf_id": upf_id, "ul_bytes": 0, "dl_bytes": 0}}, upsert=True)
    create_document("logentry", LogEntry(nf="SMF", level="INFO", message="Session established", context={"session_id": sess.session_id}))
    return {"result": "OK", "session_id": sess.session_id, "upf": upf_id, "qos": qos}


@smf.get("/health")
def smf_health():
    return HealthStatus(nf="SMF", status="HEALTHY")


# ---------------------- UPF ----------------------
@upf.get("/counters")
def upf_counters():
    docs = get_documents("upfstate", {})
    return docs


@upf.post("/simulate-traffic/{session_id}")
def simulate_traffic(session_id: str, payload: Dict[str, Any]):
    ul = int(payload.get("ul", 1000))
    dl = int(payload.get("dl", 2000))
    sess = db["pdusession"].find_one({"session_id": session_id})
    if not sess:
        raise HTTPException(404, "Session not found")
    db["pdusession"].update_one({"session_id": session_id}, {"$inc": {"ul_bytes": ul, "dl_bytes": dl}})
    db["upfstate"].update_one({"upf_id": sess.get("upf_id", "upf-1")}, {"$inc": {"ul_bytes": ul, "dl_bytes": dl}}, upsert=True)
    create_document("logentry", LogEntry(nf="UPF", level="INFO", message="Traffic simulated", context={"session_id": session_id, "ul": ul, "dl": dl}))
    return {"status": "ok"}


@upf.get("/health")
def upf_health():
    return HealthStatus(nf="UPF", status="HEALTHY")


# Register routers
app.include_router(nrf)
app.include_router(nssf)
app.include_router(udm)
app.include_router(pcf)
app.include_router(amf)
app.include_router(smf)
app.include_router(upf)


@app.get("/")
def read_root():
    return {"message": "5G Core Simulation Backend Running"}


@app.get("/test")
def test_database():
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": None,
        "database_name": None,
        "connection_status": "Not Connected",
        "collections": []
    }
    try:
        if db is not None:
            response["database"] = "✅ Available"
            response["database_url"] = "✅ Configured"
            response["database_name"] = db.name if hasattr(db, 'name') else "✅ Connected"
            response["connection_status"] = "Connected"
            collections = db.list_collection_names()
            response["collections"] = collections[:10]
            response["database"] = "✅ Connected & Working"
        else:
            response["database"] = "⚠️  Available but not initialized"
    except Exception as e:
        response["database"] = f"❌ Error: {str(e)[:50]}"

    response["database_url"] = "✅ Set" if os.getenv("DATABASE_URL") else "❌ Not Set"
    response["database_name"] = "✅ Set" if os.getenv("DATABASE_NAME") else "❌ Not Set"
    return response


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
