"""
Database Schemas for 5G Core Simulation

Each Pydantic model below maps to a MongoDB collection with the lowercase name
of the class (e.g., UE -> "ue"). These schemas define the shape of data stored
by each simulated Network Function (NF).
"""
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field
from datetime import datetime


class UE(BaseModel):
    supi: str = Field(..., description="Subscriber Permanent Identifier")
    guti: Optional[str] = Field(None, description="Globally Unique Temporary Identifier")
    plmn: str = Field(..., description="PLMN in MCC-MNC format")
    slices: List[str] = Field(default_factory=list, description="List of allowed slice IDs")
    registered: bool = Field(False, description="Registration status")
    amf_id: Optional[str] = Field(None, description="Serving AMF instance ID")
    last_seen: Optional[datetime] = Field(default=None, description="Last activity timestamp")


class Slice(BaseModel):
    slice_id: str = Field(..., description="Slice/Service Type identifier e.g., '1', 'eMBB'")
    sst: str = Field(..., description="Slice/Service Type")
    sd: Optional[str] = Field(None, description="Slice Differentiator")
    description: Optional[str] = Field(None)
    plmns: List[str] = Field(default_factory=list, description="Allowed PLMNs")


class PolicyRule(BaseModel):
    policy_id: str = Field(...)
    desc: Optional[str] = None
    qos: Dict[str, Any] = Field(default_factory=lambda: {"5qi": 9, "mbr_ul": "10Mbps", "mbr_dl": "10Mbps"})
    charging: Dict[str, Any] = Field(default_factory=dict)


class PDUSession(BaseModel):
    session_id: str = Field(..., description="Unique PDU session identifier")
    supi: str = Field(...)
    dnn: str = Field(..., description="Data Network Name")
    s_nssai: str = Field(..., description="Selected slice ID")
    smf_id: Optional[str] = Field(None)
    upf_id: Optional[str] = Field(None)
    state: str = Field("ACTIVE", description="Session state")
    qos_rules: Dict[str, Any] = Field(default_factory=dict)
    ul_bytes: int = Field(0)
    dl_bytes: int = Field(0)


class NFService(BaseModel):
    nf_type: str = Field(..., description="NF type: AMF/SMF/UPF/NRF/NSSF/PCF/UDM")
    nf_id: str = Field(..., description="Instance identifier")
    status: str = Field("HEALTHY")
    api_base: str = Field(..., description="Base URL for the NF")
    capabilities: List[str] = Field(default_factory=list)


class LogEntry(BaseModel):
    nf: str = Field(..., description="NF producing the log")
    level: str = Field("INFO")
    message: str
    context: Dict[str, Any] = Field(default_factory=dict)


class HealthStatus(BaseModel):
    nf: str
    status: str = "HEALTHY"
    details: Dict[str, Any] = Field(default_factory=dict)
