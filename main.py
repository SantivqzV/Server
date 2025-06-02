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
from datetime import datetime

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
    color_index: int

class ConfirmPlacementRequest(BaseModel):
    cubby_id: int

class RegisterUserRequest(BaseModel):
    name: str
    username: str
    email: str
    password: str
    color_hex: str
    color_index: int

class LoginRequest(BaseModel):
    identifier: str
    password: str

class UpdateColorRequest(BaseModel):
    username: str
    color_hex: str
    color_index: int

class UserExistsRequest(BaseModel):
    uuid: str

class ReleaseCubbyRequest(BaseModel):
    cubby_id: int

# Helper to send MQTT message with full debug
def send_mqtt_message(cubby_id: int, color_index: int, remaining_items: int):
    topic = f"cubbie/{cubby_id}/item"
    colors = ["red", "green", "blue", "yellow", "cyan", "magenta"]
    color_name = colors[color_index]
    payload = {
        "status": "ASSIGNED",
        "color": color_name,
        "remaining_items": remaining_items,
        "paqueteria": "coppel",
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


    # 2. Filter out orders with cubby in progress
    filtered_orders = []

    for order in possible_orders:
        order_data = supabase.table("orders")\
            .select("orderid, cubbyid, remaining_items")\
            .eq("orderid", order)\
            .single()\
            .execute()

        if not order_data.data:
            continue

        cubby = order_data.data.get("cubbyid")
        if cubby is not None:
            cubby_data = supabase.table("cubbies")\
                .select("in_progress")\
                .eq("cubbyid", cubby)\
                .single()\
                .execute()
            if cubby_data.data and cubby_data.data["in_progress"]:
                continue  # Skip if cubby is currently in use

        filtered_orders.append(order_data.data)

    if not filtered_orders:
        raise HTTPException(status_code=409, detail="All matching orders are currently in progress.")

    # Sort by remaining_items ascending
    filtered_orders.sort(key=lambda x: x["remaining_items"])
    best_order = filtered_orders[0]

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
        .update({"scanned": True,
                 "scanned_at": datetime.utcnow().strftime("%Y-%m-%d")
                })\
        .eq("orderid", order_id)\
        .eq("sku", payload.sku)\
        .execute()

    # 7. Get product name for response
    product_res = supabase.table("products").select("name").eq("sku", payload.sku).single().execute()
    product_name = product_res.data["name"] if product_res.data else "Unknown Product"

    # 8. Color for MQTT
    color_index = payload.color_index
    send_mqtt_message(cubby_id, color_index, (remaining_items - 1))

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

@app.get("/get-orders")
async def get_orders():
    items_res = supabase.table("order_items").select("sku").execute()
    if not items_res.data:
        raise HTTPException(status_code=404, detail="No orders found")
    
    from collections import Counter
    sku_counts = Counter([item["sku"] for item in items_res.data])

    sku_list = list(sku_counts.keys())
    products_res = supabase.table("products").select("sku, name").in_("sku", sku_list).execute()
    sku_to_name = {prod["sku"]: prod["name"] for prod in products_res.data} if products_res.data else {}

    result = []
    for sku, count in sku_counts.items():
        result.append({
            "sku": sku,
            "name": sku_to_name.get(sku, "Unknown Product"),
            "count": count
        })

    result.sort(key=lambda x: x["count"], reverse=True)  # Sort by SKU
    return result

@app.get("/get-cubbies")
async def get_cubbies():
    cubbies_res = supabase.table("cubbies").select("*").execute()
    if not cubbies_res.data:
        raise HTTPException(status_code=404, detail="No cubbies found")
    
    cubbies = cubbies_res.data
    result = []
    for cubby in cubbies:
        cubby_info = dict(cubby)
        cubby_id = cubby.get("cubbyid")
        # Use limit(1) to avoid errors if no order is found
        order_res = supabase.table("orders").select("orderid, remaining_items").eq("cubbyid", cubby_id).limit(1).execute()
        if order_res.data and len(order_res.data) > 0 and order_res.data[0].get("orderid") is not None:
            cubby_info["orderid"] = order_res.data[0].get("orderid", 0)
            cubby_info["remaining_items"] = order_res.data[0].get("remaining_items", None)
        else:
            cubby_info["orderid"] = 0
            cubby_info["remaining_items"] = None
        
        result.append(cubby_info)

    result.sort(key=lambda x: x.get("cubbyid", 0))
    return result

@app.post("/register-user")
async def register_user(payload: RegisterUserRequest):
    # Check if user already exists by username OR email
    existing_user = supabase.table("users") \
        .select("*") \
        .or_(f"username.eq.{payload.username},email.eq.{payload.email}") \
        .limit(1) \
        .execute()
    if existing_user.data and len(existing_user.data) > 0:
        raise HTTPException(status_code=400, detail="User already registered")

    # Insert new user (Supabase will generate the id)
    user_data = payload.dict()
    supabase.table("users").insert(user_data).execute()

    logging.info(f"✅ User {payload.username} registered successfully.")
    return {"message": f"User {payload.username} registered successfully."}

@app.post("/login")
async def login(payload: LoginRequest):
    # Check if user exists by username OR email
    user_res = supabase.table("users") \
        .select("*") \
        .or_(f"username.eq.{payload.identifier},email.eq.{payload.identifier}") \
        .limit(1) \
        .execute()
    if not user_res.data or len(user_res.data) == 0:
        return {"success": False}

    user_data = user_res.data[0]

    if user_data.get("password") != payload.password:
        return {"success": False}
    
    user_data.pop("password", None)  

    logging.info(f"✅ User {user_data.get('username')} logged in successfully.")
    return {"success": True, "user": user_data}

@app.patch("/update-color")
async def update_color(payload: UpdateColorRequest):
    # Check if user exists
    user_res = supabase.table("users").select("*").eq("username", payload.username).single().execute()
    if not user_res.data:
        raise HTTPException(status_code=404, detail="User not found")

    # Update color
    supabase.table("users").update({
        "color_hex": payload.color_hex,
        "color_index": payload.color_index
    }).eq("username", payload.username).execute()

    logging.info(f"✅ User {payload.username} color updated to {payload.color_hex}.")
    return {"message": f"User {payload.username} color updated to {payload.color_hex}."}

@app.get("/get-used-colors")
async def get_users():
    users_res = supabase.table("users").select("color_hex, color_index").execute()
    if not users_res.data:
        raise HTTPException(status_code=404, detail="No users found")
    
    return users_res.data

@app.post("/me")
async def me(payload: UserExistsRequest):
    user_res = supabase.table("users").select("*").eq("id", payload.uuid).limit(1).execute()
    if user_res.data and len(user_res.data) > 0:
        return {"exists": True, "data": user_res.data}
    return {"exists": False}

@app.get("/get-operators")
async def get_operators():
    operators_res = supabase.table("users").select("*").execute()
    if not operators_res.data:
        raise HTTPException(status_code=404, detail="No operators found")
    
    return operators_res.data

@app.post("/release-cubby")
async def release_cubby(payload: ReleaseCubbyRequest):
    cubby_id = payload.cubby_id

    # Verificar que el cubby existe
    cubby_res = supabase.table("cubbies").select("*").eq("cubbyid", cubby_id).single().execute()
    if not cubby_res.data:
        raise HTTPException(status_code=404, detail="Cubby not found")

    # Actualizar estado
    supabase.table("cubbies").update({
        "occupied": False,
        "in_progress": False
    }).eq("cubbyid", cubby_id).execute()

    # (Opcional) desvincularlo de una orden
    supabase.table("orders").update({
        "cubbyid": None
    }).eq("cubbyid", cubby_id).execute()

    logging.info(f"🟩 Cubby {cubby_id} liberado manualmente.")
    return {"message": f"Cubby {cubby_id} fue liberado exitosamente."}

# get scanned items with dates
@app.get("/get-scanned-items")
async def get_scanned_items():
    scanned_items_res = supabase.table("order_items").select("*").eq("scanned", True).execute()
    if not scanned_items_res.data:
        raise HTTPException(status_code=404, detail="No scanned items found")
    
    return scanned_items_res.data