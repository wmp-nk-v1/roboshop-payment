import os
import json
import uuid
import time
import logging
import aio_pika
import httpx
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("payment")

app = FastAPI(title="RoboShop Payment Service")

@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    logger.info(json.dumps({
        "method": request.method,
        "path": request.url.path,
        "status": response.status_code,
        "latency_ms": round((time.time() - start) * 1000, 3)
    }))
    return response

AMQP_HOST = os.getenv("AMQP_HOST", "rabbitmq")
AMQP_USER = os.getenv("AMQP_USER", "guest")
AMQP_PASS = os.getenv("AMQP_PASS", "guest")
CART_URL  = os.getenv("CART_URL", "http://cart:8003")
USER_URL  = os.getenv("USER_URL", "http://user:8001")

EXCHANGE    = "roboshop"
ROUTING_KEY = "orders"

_amqp_connection = None
_amqp_channel    = None


async def get_amqp_channel():
    global _amqp_connection, _amqp_channel
    if _amqp_connection is None or _amqp_connection.is_closed:
        _amqp_connection = await aio_pika.connect_robust(
            host=AMQP_HOST, login=AMQP_USER, password=AMQP_PASS
        )
        _amqp_channel = await _amqp_connection.channel()
        await _amqp_channel.declare_exchange(EXCHANGE, aio_pika.ExchangeType.DIRECT, durable=True)
        queue = await _amqp_channel.declare_queue("orders", durable=True)
        exchange = await _amqp_channel.get_exchange(EXCHANGE)
        await queue.bind(exchange, ROUTING_KEY)
        logger.info("Connected to RabbitMQ")
    return _amqp_channel


class PaymentRequest(BaseModel):
    userId: str
    cityId: int


@app.on_event("startup")
async def startup():
    for attempt in range(30):
        try:
            await get_amqp_channel()
            return
        except Exception as e:
            logger.warning(f"RabbitMQ connection attempt {attempt+1}/30 failed: {e}")
            await __import__("asyncio").sleep(2)
    raise Exception("Failed to connect to RabbitMQ")


@app.on_event("shutdown")
async def shutdown():
    if _amqp_connection and not _amqp_connection.is_closed:
        await _amqp_connection.close()


@app.get("/health")
def health():
    return {"status": "OK", "service": "payment"}


@app.post("/payment/process")
async def process_payment(request: PaymentRequest):
    async with httpx.AsyncClient() as client:
        try:
            user_resp = await client.get(f"{USER_URL}/validate/{request.userId}")
            if user_resp.status_code != 200:
                raise HTTPException(status_code=400, detail="Invalid user")
            user = user_resp.json()
        except httpx.RequestError:
            raise HTTPException(status_code=503, detail="User service unavailable")

        try:
            cart_resp = await client.get(f"{CART_URL}/cart/{request.userId}")
            if cart_resp.status_code != 200:
                raise HTTPException(status_code=400, detail="Failed to get cart")
            cart = cart_resp.json()
        except httpx.RequestError:
            raise HTTPException(status_code=503, detail="Cart service unavailable")

    if not cart.get("items"):
        raise HTTPException(status_code=400, detail="Cart is empty")

    total          = sum(item["price"] * item["quantity"] for item in cart["items"])
    transaction_id = f"TXN-{uuid.uuid4().hex[:8].upper()}"

    order_event = {
        "userId":        request.userId,
        "userEmail":     user.get("email", ""),
        "userName":      user.get("firstName", "Customer"),
        "items":         cart["items"],
        "total":         total,
        "cityId":        request.cityId,
        "transactionId": transaction_id,
        "status":        "PAID",
    }

    try:
        channel  = await get_amqp_channel()
        exchange = await channel.get_exchange(EXCHANGE)
        await exchange.publish(
            aio_pika.Message(
                body=json.dumps(order_event).encode(),
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            ),
            routing_key=ROUTING_KEY,
        )
        logger.info(f"Payment processed: {transaction_id} for user {request.userId}")
    except Exception as e:
        logger.error(f"Failed to publish order event: {e}")
        raise HTTPException(status_code=503, detail="Order event failed — please retry")

    async with httpx.AsyncClient() as client:
        try:
            await client.delete(f"{CART_URL}/cart/{request.userId}")
        except Exception:
            logger.warning("Failed to clear cart after payment")

    return {
        "status":        "SUCCESS",
        "transactionId": transaction_id,
        "total":         total,
    }
