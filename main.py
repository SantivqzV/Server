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
MQTT_USER = "Servers1"
MQTT_PASS = "Servers1"

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

class ConfirmPlacementRequest(BaseModel):
    cubby_id: int

# Helper to send MQTT message with full debug
def send_mqtt_message(cubby_id: int, color_index: int):
    topic = f"cubbie/{cubby_id}/item"
    colors = ["red", "green", "blue", "yellow", "cyan", "magenta"]
    color_name = colors[color_index]
    payload = {
        "status": "ASSIGNED",
        "color": color_name,
        "remaining_items": 3  # optional, could be dynamic later
    }
    try:
        logging.info(f"Publishing MQTT to '{topic}' with payload '{payload}'")
        result = mqtt_client.publish(topic, json.dumps(payload))
        if result.rc == mqtt.MQTT_ERR_SUCCESS:
            logging.info(f"✅ MQTT Published to {topic}")
        else:
            logging.error(f"❌ MQTT Publish failed with code {result.rc}")
    except Exception as e:
        logging.error(f"⚠️ MQTT publish exception: {e}")


# POST /scan-item endpoint
@app.post("/scan-item")
async def scan_item(payload: ScanItemRequest):
    logging.info(f"Received payload: {payload}")

    # 1. Find orders that have this SKU and it's not yet scanned
    item_res = supabase.table("order_items")\
        .select("orderid")\
        .eq("sku", payload.sku)\
        .eq("scanned", False)\
        .execute()

    if not item_res.data:
        raise HTTPException(status_code=404, detail="SKU not pending in any order")

    possible_orders = [row["orderid"] for row in item_res.data]

    # 2. Find the best order (fewest remaining_items)
    order_query = supabase.table("orders")\
        .select("orderid, cubbyid, remaining_items")\
        .in_("orderid", possible_orders)\
        .order("remaining_items")\
        .limit(1)\
        .execute()

    if not order_query.data:
        raise HTTPException(status_code=404, detail="No matching orders with pending items")

    best_order = order_query.data[0]
    order_id = best_order["orderid"]
    cubby_id = best_order.get("cubbyid")
    remaining_items = best_order.get("remaining_items")

    # 3. Check if assigned cubby is in progress
    if cubby_id is not None:
        cubby_check = supabase.table("cubbies")\
            .select("in_progress")\
            .eq("cubbyid", cubby_id)\
            .single()\
            .execute()

        if not cubby_check.data:
            raise HTTPException(status_code=404, detail="Cubby not found")

        if cubby_check.data["in_progress"]:
            raise HTTPException(
                status_code=409,
                detail=f"Cubby {cubby_id} is still in progress. Wait for confirmation before placing another item."
            )
        
        # Mark cubby as in progress because we are about to assign a new item
        supabase.table("cubbies").update({
            "occupied": True,
            "in_progress": True
        }).eq("cubbyid", cubby_id).execute()



    # 4. If no cubby assigned yet, find and assign one
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

        # Update order with cubby
        supabase.table("orders").update({"cubbyid": cubby_id}).eq("orderid", order_id).execute()

        # Mark cubby as occupied
        supabase.table("cubbies").update({
            "occupied": True,
            "in_progress": True
            }).eq("cubbyid", cubby_id).execute()

    # 5. Mark item as scanned
    supabase.table("order_items")\
        .update({"scanned": True})\
        .eq("orderid", order_id)\
        .eq("sku", payload.sku)\
        .execute()

    # 6. Decrease remaining items
    supabase.table("orders")\
        .update({"remaining_items": remaining_items - 1})\
        .eq("orderid", order_id)\
        .execute()

    # 7. Get product name for response
    product_res = supabase.table("products").select("name").eq("sku", payload.sku).single().execute()
    product_name = product_res.data["name"] if product_res.data else "Unknown Product"

    # 8. Color for MQTT
    color_index = random.randint(0, 5)
    send_mqtt_message(cubby_id, color_index)

    return {"assignedCubby": cubby_id, "productName": product_name, "colorIndex": color_index}

@app.post("/confirm-placement")
async def confirm_placement(payload: ConfirmPlacementRequest):
    cubby_id = payload.cubby_id

    # 1. Check cubby exists
    cubby_res = supabase.table("cubbies").select("*").eq("cubbyid", cubby_id).single().execute()
    if not cubby_res.data:
        raise HTTPException(status_code=404, detail="Cubby not found")

    # 2. Set in_progress = FALSE
    supabase.table("cubbies").update({
        "in_progress": False
    }).eq("cubbyid", cubby_id).execute()

    logging.info(f"✅ Cubby {cubby_id} placement confirmed.")
    return {"message": f"Cubby {cubby_id} confirmed"}