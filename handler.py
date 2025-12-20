import os
import json
import uuid
import base64
import logging
import time
import urllib.request
import urllib.parse
import websocket
import runpod
import binascii
from PIL import Image
from io import BytesIO

# ================== LOGGING ==================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ================== GLOBALS ==================

SERVER_ADDRESS = os.getenv("SERVER_ADDRESS", "127.0.0.1")
CLIENT_ID = str(uuid.uuid4())

COMFY_INPUT_DIR = "/workspace/ComfyUI/input"

# ================== COMFY HELPERS ==================

def wait_for_comfyui(timeout=180):
    url = f"http://{SERVER_ADDRESS}:8188/"
    for _ in range(timeout):
        try:
            urllib.request.urlopen(url, timeout=3)
            logger.info("✅ ComfyUI HTTP ready")
            return
        except Exception:
            time.sleep(1)
    raise RuntimeError("ComfyUI not reachable")


def save_image_as_rgb_jpeg(data_input, output_filename="input_image.jpg"):

    os.makedirs(COMFY_INPUT_DIR, exist_ok=True)
    out_path = os.path.join(COMFY_INPUT_DIR, output_filename)

    # 1. получить байты изображения
    if isinstance(data_input, str) and os.path.exists(data_input):
        logger.info(f"📁 Image path provided: {data_input}")
        with open(data_input, "rb") as f:
            raw_bytes = f.read()
    else:
        try:
            if "," in data_input:
                data_input = data_input.split(",", 1)[1]
            raw_bytes = base64.b64decode(data_input, validate=True)
        except (binascii.Error, ValueError) as e:
            raise RuntimeError("Invalid image_path: not valid base64 or path") from e

    # 2. открыть через PIL (КРИТИЧЕСКИ ВАЖНО)
    try:
        img = Image.open(BytesIO(raw_bytes))
        img.load()
    except Exception as e:
        raise RuntimeError("PIL failed to open input image") from e

    # 3. привести к RGB (убирает alpha, palette, CMYK и т.д.)
    if img.mode != "RGB":
        img = img.convert("RGB")

    # 4. сохранить как JPEG
    img.save(
        out_path,
        format="JPEG",
        quality=100,
        subsampling=0,
        optimize=False
    )

    logger.info(f"🖼 Image normalized and saved as JPEG: {out_path}")
    return out_path


def queue_prompt(prompt):
    url = f"http://{SERVER_ADDRESS}:8188/prompt"
    payload = {"prompt": prompt, "client_id": CLIENT_ID}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data)
    return json.loads(urllib.request.urlopen(req).read())


def get_history(prompt_id):
    url = f"http://{SERVER_ADDRESS}:8188/history/{prompt_id}"
    with urllib.request.urlopen(url) as response:
        return json.loads(response.read())


def get_image(filename, subfolder, folder_type):
    url = f"http://{SERVER_ADDRESS}:8188/view"
    params = {
        "filename": filename,
        "subfolder": subfolder,
        "type": folder_type,
    }
    query = urllib.parse.urlencode(params)
    with urllib.request.urlopen(f"{url}?{query}") as response:
        return response.read()


def get_images(ws, workflow):
    prompt_id = queue_prompt(workflow)["prompt_id"]
    logger.info(f"🚀 prompt_id = {prompt_id}")

    while True:
        msg = ws.recv()
        if isinstance(msg, str):
            data = json.loads(msg)
            if data["type"] == "executing":
                if data["data"]["node"] is None and data["data"]["prompt_id"] == prompt_id:
                    break

    history = get_history(prompt_id)[prompt_id]
    outputs = {}

    for node_id, node_output in history["outputs"].items():
        images = []
        if "images" in node_output:
            for img in node_output["images"]:
                raw = get_image(
                    img["filename"],
                    img["subfolder"],
                    img["type"],
                )
                images.append(base64.b64encode(raw).decode("utf-8"))
        outputs[node_id] = images

    return outputs

# ================== HANDLER ==================

def handler(job):
    job_input = job.get("input", {})
    logger.info("📥 Job HUY TEST received")

    image_input = job_input.get("image_path")
    workflow = job_input.get("workflow")

    if not image_input:
        return {"error": "image_path is required"}
    if not workflow:
        return {"error": "workflow is required"}

    # 🔥 КЛЮЧЕВОЙ ШАГ
    image_path = save_image_as_rgb_jpeg(image_input)

    # LoadImage ВСЕГДА получает путь
    workflow["1"]["inputs"]["image"] = image_path

    logger.info(f"📂 HUY TEST input files: {os.listdir(COMFY_INPUT_DIR)}")

    wait_for_comfyui()

    ws = websocket.WebSocket()
    ws.connect(f"ws://{SERVER_ADDRESS}:8188/ws?clientId={CLIENT_ID}")

    try:
        images = get_images(ws, workflow)
    finally:
        ws.close()

    for node_id in images:
        if images[node_id]:
            logger.info("✅ Image generated")
            return {"image": images[node_id][0]}

    return {"error": "No image output"}

# ================== START ==================

runpod.serverless.start({"handler": handler})
