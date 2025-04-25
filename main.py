from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client
import os
from dotenv import load_dotenv

# Cargar variables de entorno
load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

# Inicializar FastAPI y CORS
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Modelos de datos
class AssignOrderPayload(BaseModel):
    orderId: str
    itemCount: int

class ScanItemPayload(BaseModel):
    itemId: str
    orderId: str
    operatorId: str
    cubbyId: str

class CompleteOrderPayload(BaseModel):
    orderId: str

class LowBatteryAlert(BaseModel):
    moduleId: str
    batteryLevel: float

class CommunicationErrorAlert(BaseModel):
    moduleId: str
    message: str

# Endpoint para asignar un cubby a una orden (sin duplicar en DB)
@app.post("/assign-order")
async def assign_order(payload: AssignOrderPayload):
    # Buscar el primer cubby disponible
    res = supabase.table("cubbies") \
        .select("*") \
        .gte("capacity", payload.itemCount) \
        .order("cubbyid") \
        .limit(1) \
        .execute()
    if not res.data:
        raise HTTPException(status_code=400, detail="No hay cubetas disponibles")
    cubby_id = res.data[0]["cubbyid"]

    # Solo insertar la orden si no existe
    existing = supabase.table("orders") \
        .select("orderid") \
        .eq("orderid", payload.orderId) \
        .execute()
    if not existing.data:
        supabase.table("orders").insert({
            "orderid": payload.orderId,
            "totalitems": payload.itemCount,
            "remainingitems": payload.itemCount
        }).execute()

    return {"assignedCubby": cubby_id}

# Endpoint para registrar escaneo de ítem
@app.post("/scan-item")
async def scan_item(payload: ScanItemPayload):
    existing = supabase.table("items") \
        .select("itemid") \
        .eq("orderid", payload.orderId) \
        .limit(1) \
        .execute()

    assigned_cubby = payload.cubbyId  # por defecto viene null o vacío del cliente
    if not existing.data:
        # Buscar cubby libre según capacidad
        cub = supabase.table("cubbies") \
            .select("*") \
            .gte("capacity", 1) \
            .order("cubbyid") \
            .limit(1) \
            .execute()
        if not cub.data:
            raise HTTPException(400, "No hay cubetas disponibles para asignar")
        assigned_cubby = cub.data[0]["cubbyid"]
        # (Opcional) podrías llevar registro de la asignación en otra tabla si quieres

    supabase.table("items").insert({
        "itemid": payload.itemId,
        "orderid": payload.orderId,
        "operatorid": payload.operatorId,
        "cubbyid": assigned_cubby,
        "status": "escaneado"
    }).execute()

    supabase.table("orders") \
        .update({"remainingitems": supabase.func("remainingitems") - 1}) \
        .eq("orderid", payload.orderId) \
        .execute()
    supabase.table("cubbies") \
        .update({"numitems": supabase.func("numitems") + 1}) \
        .eq("cubbyid", assigned_cubby) \
        .execute()

    return {
        "status": "item registrado",
        "assignedCubby": assigned_cubby if not existing.data else None
    }


# Endpoint para completar orden
@app.post("/complete-order")
async def complete_order(payload: CompleteOrderPayload):
    order = supabase.table("orders").select("*").eq("orderid", payload.orderId).single().execute()
    if not order.data or order.data["remainingitems"] != 0:
        raise HTTPException(status_code=400, detail="La orden aún no está completa")
    items = supabase.table("items").select("cubbyid").eq("orderid", payload.orderId).execute()
    cubby_ids = {row["cubbyid"] for row in items.data}
    for cb in cubby_ids:
        supabase.table("cubbies").update({"numitems": 0}).eq("cubbyid", cb).execute()
    return {"status": "orden completada"}

# Alerts
@app.post("/alerts/low-battery")
async def low_battery(alert: LowBatteryAlert):
    print(f"Batería baja: {alert.moduleId} - {alert.batteryLevel}%")
    return {"status": "recibido"}

@app.post("/alerts/communication-error")
async def comm_error(alert: CommunicationErrorAlert):
    print(f"Error de comunicación en {alert.moduleId}: {alert.message}")
    return {"status": "recibido"}

# Endpoints para carga inicial desde simulador
@app.get("/orders")
async def get_orders():
    res = supabase.table("orders").select("*").execute()
    return res.data

@app.get("/cubbies")
async def get_cubbies():
    res = supabase.table("cubbies").select("*").execute()
    return res.data

@app.get("/items")
async def get_items():
    res = supabase.table("items").select("*").execute()
    return res.data
