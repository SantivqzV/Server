from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client
import paho.mqtt.publish as publish
import os
from dotenv import load_dotenv
import random
import logging

# Load environment variables
load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
MQTT_BROKER = os.getenv("MQTT_BROKER")
MQTT_PORT = int(os.getenv("MQTT_PORT", 1883))
MQTT_USER = os.getenv("MQTT_USER")
MQTT_PASS = os.getenv("MQTT_PASS")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

logging.basicConfig(level=logging.INFO)

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
    orderId: str

# Helper to send MQTT message with authentication
def send_mqtt_message(cubby_id: int, color: int):
    topic = f"cubbie/{cubby_id}/item"
    payload = str(color)  # Send color index as payload
    publish.single(
        topic,
        payload=payload,
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
    logging.info(f"Received payload: {payload}")
    # 1. Get product name
    product_res = supabase.table("products").select("name").eq("sku", payload.sku).single().execute()
    logging.info(f"Product query result: {product_res}")
    if not product_res.data:
        raise HTTPException(status_code=404, detail="SKU not found in product catalog")
    product_name = product_res.data["name"]

    # 2. Check if order already has assigned cubby
    order_res = supabase.table("orders").select("cubbyid").eq("orderid", payload.orderId).single().execute()
    if not order_res.data:
        raise HTTPException(status_code=404, detail="Order not found")

    cubby_id = order_res.data.get("cubbyid")

    # 3. If no cubby assigned yet, find one and assign it
    if cubby_id is None:
        # Fix for the .order() method
        cubby_res = supabase.table("cubbies")\
            .select("cubbyid")\
            .eq("occupied", False)\
            .order("cubbyid")\
            .limit(1)\
            .execute()

        if not cubby_res.data:
            raise HTTPException(status_code=400, detail="No available cubbies")

        cubby_id = cubby_res.data[0]["cubbyid"]

        # Update orders table
        supabase.table("orders").update({"cubbyid": cubby_id}).eq("orderid", payload.orderId).execute()

        # Mark cubby as occupied
        supabase.table("cubbies").update({"occupied": True}).eq("cubbyid", cubby_id).execute()

    # 4. Mark item as scanned
    supabase.table("order_items").update({"scanned": True}).eq("orderid", payload.orderId).eq("sku", payload.sku).execute()

    # 5. Assign a random color index (0 to 5)
    color_index = random.randint(0, 5)

    # 6. Send MQTT message to cubby with color
    send_mqtt_message(cubby_id, color_index)

    # 7. Respond with assigned cubby, product name, and color
    return {"assignedCubby": cubby_id, "productName": product_name, "colorIndex": color_index}
