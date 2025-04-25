from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client
import paho.mqtt.publish as publish
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
MQTT_BROKER = os.getenv("MQTT_BROKER")
MQTT_PORT = int(os.getenv("MQTT_PORT", 1883))
MQTT_USER = os.getenv("MQTT_USER")
MQTT_PASS = os.getenv("MQTT_PASS")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

# Initialize FastAPI
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Pydantic model for scanning
class ScanItemRequest(BaseModel):
    sku: str
# Helper to send MQTT message with authentication
def send_mqtt_message(cubby_id: int, sku: str):
    topic = f"cubbie/{cubby_id}/item"
    publish.single(
        topic,
        payload=sku,
        hostname=MQTT_BROKER,
        port=MQTT_PORT,
        auth={
            "username": MQTT_USER,
            "password": MQTT_PASS
        }
    )

# POST /scan-item endpoint
@app.post("/scan-item")
async def scan_item(payload: ScanItemRequest):
    # 1. Find the nearest available cubby
    cubby_res = supabase.table("cubbies")\
        .select("cubbyid")\
        .eq("occupied", False)\
        .order("cubbyid", asc=True)\
        .limit(1)\
        .execute()

    if not cubby_res.data:
        raise HTTPException(status_code=400, detail="No available cubbies")

    cubby_id = cubby_res.data[0]["cubbyid"]

    # 2. Mark cubby as occupied with this SKU
    supabase.table("cubbies").update({
        "occupied": True,
        "sku": payload.sku
    }).eq("cubbyid", cubby_id).execute()

    # 3. Insert the item into 'items' table
    supabase.table("items").insert({
        "sku": payload.sku,
        "cubbyid": cubby_id
    }).execute()

    # 4. Send MQTT message to cubby
    send_mqtt_message(cubby_id, payload.sku)

    return {"assignedCubby": cubby_id, "message": f"SKU {payload.sku} assigned to cubby {cubby_id}"}
