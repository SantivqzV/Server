from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client
import paho.mqtt.client as mqtt
import os
from dotenv import load_dotenv
import random
import json
import logging

# Load environment variables
load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
MQTT_BROKER = "8599a322d7a3418cae7d8d51f111fb87.s1.eu.hivemq.cloud"
MQTT_PORT = 8883
MQTT_USER = "santivqz"
MQTT_PASS = "Coppel2025"

# Initialize MQTT client
mqtt_client = mqtt.Client()
mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)
mqtt_client.tls_set()
mqtt_client.connect(MQTT_BROKER, MQTT_PORT)
mqtt_client.loop_start()

# Define MQTT event callbacks
def on_connect(client, userdata, flags, rc):
    if rc == 0:
        logging.info("Connected to MQTT broker")
    else:
        logging.error(f"Failed to connect to MQTT broker, return code {rc}")

def on_disconnect(client, userdata, rc):
    logging.info("Disconnected from MQTT broker")

# Attach callbacks
mqtt_client.on_connect = on_connect
mqtt_client.on_disconnect = on_disconnect

# Initialize Supabase client
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

# Configure logging
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

# Helper to send MQTT message with full debug
def send_mqtt_message(cubby_id: int, color_index: int):
    topic = f"cubbie/{cubby_id}/item"
    colors = ["red", "green", "blue", "yellow", "cyan", "magenta"]
    color_name = colors[color_index]  # Map the color index to a color name
    payload = {
        "status": "ASSIGNED",
        "color": color_name,
        "remaining_items": 3
    }
    try:
        logging.info(f"Preparing to publish MQTT message to topic '{topic}' with payload '{payload}'")
        result = mqtt_client.publish(topic, json.dumps(payload))
        status = result.rc
        if status == mqtt.MQTT_ERR_SUCCESS:
            logging.info(f"✅ Successfully published to {topic}")
        else:
            logging.error(f"❌ Failed to publish to {topic}, status code: {status}")
    except Exception as e:
        logging.error(f"⚠️ Exception during MQTT publish: {e}")


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