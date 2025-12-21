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

# ================== GCS ==================

from google.cloud import storage

GCS_BUCKET = os.getenv("GCS_BUCKET", "generations-reserve")

_storage_client = storage.Client()

def upload_to_gcs(image_bytes, content_type="image/jpeg"):
    bucket = _storage_client.bucket(GCS_BUCKET)

    filename = f"results/{uuid.uuid4().hex}.jpg"
    blob = bucket.blob(filename)

    blob.upload_from_string(
        image_bytes,
        content_type=content_type
    )

    return f"https://storage.googleapis.com/{GCS_BUCKET}/{filename}"

# ================== LOGGING ==================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DEV = os.getenv("DEV", "UNSET")
TEST = os.getenv("TEST", "UNSET")

logger.info("=" * 80)
logger.info("🚀🚀🚀  STARTING HANDLER")
logger.info(f"🔥🔥🔥  DEV  = >>> {DEV} <<<")
logger.info(f"🔥🔥🔥  TEST = >>> {TEST} <<<")
logger.info("=" * 80)

# ================== GLOBALS ==================

SERVER_ADDRESS = os.getenv("SERVER_ADDRESS", "127.0.0.1")
CLIENT_ID = str(uuid.uuid4())

COMFY_INPUT_DIR = "/workspace/ComfyUI/input"

DEFAULT_WORKFLOW_PATH = os.path.join(
    os.path.dirname(__file__),
    "workflow.json"
)

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
    raise RuntimeError("ComfyUI is not reachable.")

# ================== WORKFLOW ==================

def load_workflow(client_workflow=None):
    if client_workflow:
        logger.info("🧩 Using client-provided workflow")
        return client_workflow

    logger.info("🧩 Using default workflow.json")
    try:
        with open(DEFAULT_WORKFLOW_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        raise RuntimeError("Failed to load default workflow.json.") from e

# ================== IMAGE LOADING ==================

def load_image_bytes(image_url=None, image_base64=None):
    if image_url:
        logger.info(f"🌐 Downloading image from URL: {image_url}")
        try:
            with urllib.request.urlopen(image_url, timeout=15) as response:
                return response.read()
        except Exception as e:
            raise RuntimeError("Failed to download image from 'image_url'.") from e

    if image_base64:
        try:
            if "," in image_base64:
                image_base64 = image_base64.split(",", 1)[1]
            return base64.b64decode(image_base64, validate=True)
        except (binascii.Error, ValueError) as e:
            raise RuntimeError("Invalid 'image_base64'.") from e

    raise RuntimeError("No image source provided.")

def save_image_bytes_as_jpeg(raw_bytes):
    os.makedirs(COMFY_INPUT_DIR, exist_ok=True)
    out_path = os.path.join(COMFY_INPUT_DIR, "input_image.jpg")

    try:
        img = Image.open(BytesIO(raw_bytes))
        img.load()
    except Exception as e:
        raise RuntimeError("Invalid image file.") from e

    if img.mode != "RGB":
        img = img.convert("RGB")

    img.save(out_path, format="JPEG", quality=100, subsampling=0)
    return out_path, img.size

# ================== COMFY API ==================

def queue_prompt(prompt):
    url = f"http://{SERVER_ADDRESS}:8188/prompt"
    payload = {"prompt": prompt, "client_id": CLIENT_ID}
    data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"}
    )

    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())

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
    images = []

    for node_output in history["outputs"].values():
        if "images" in node_output:
            for img in node_output["images"]:
                raw = get_image(img["filename"], img["subfolder"], img["type"])
                images.append(raw)

    return images

# ================== IMAGE POSTPROCESS ==================

def process_output_image(
    raw_bytes,
    target_size,
    original_size=True,
    quality=90
):
    img = Image.open(BytesIO(raw_bytes))

    if img.mode != "RGB":
        img = img.convert("RGB")

    if original_size and img.size != target_size:
        img = img.resize(target_size, Image.LANCZOS)

    buf = BytesIO()
    img.save(
        buf,
        format="JPEG",
        quality=quality,
        subsampling=0,
        optimize=True
    )
    return buf.getvalue()

# ================== HANDLER ==================

def handler(job):
    job_input = job.get("input", {})
    logger.info("📥 Job received")

    image_url = job_input.get("image_url")
    image_base64 = job_input.get("image_base64")
    client_workflow = job_input.get("workflow")

    original_size = job_input.get("original_size", True)
    image_quality = int(job_input.get("image_quality", 90))
    image_quality = max(0, min(100, image_quality))

    if not image_url and not image_base64:
        return {"error": "Image source missing."}

    workflow = load_workflow(client_workflow)
    raw_bytes = load_image_bytes(image_url=image_url, image_base64=image_base64)
    image_path, input_size = save_image_bytes_as_jpeg(raw_bytes)

    workflow["1"]["inputs"]["image"] = image_path

    wait_for_comfyui()

    ws = websocket.WebSocket()
    ws.connect(f"ws://{SERVER_ADDRESS}:8188/ws?clientId={CLIENT_ID}")

    try:
        images = get_images(ws, workflow)
    finally:
        ws.close()

    if not images:
        return {"error": "No images generated."}

    final_bytes = process_output_image(
        images[0],
        target_size=input_size,
        original_size=original_size,
        quality=image_quality
    )

    image_url = upload_to_gcs(final_bytes)

    return {
        "image_url": image_url
    }

# ================== START ==================

runpod.serverless.start({"handler": handler})
