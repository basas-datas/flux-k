import copy
import os
import base64
import binascii
import json
import logging
import uuid
import urllib.request
import urllib.parse
import websocket
import runpod

# ================== LOGGING ==================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ================== CUDA CHECK ==================

def check_cuda_availability():
    """Check CUDA availability and configure environment."""
    try:
        import torch
        if torch.cuda.is_available():
            logger.info("✅ CUDA is available and working")
            os.environ["CUDA_VISIBLE_DEVICES"] = "0"
            return True
        else:
            logger.error("❌ CUDA is not available")
            raise RuntimeError("CUDA is required but not available")
    except Exception as e:
        logger.error(f"❌ CUDA check failed: {e}")
        raise RuntimeError(f"CUDA initialization failed: {e}")

try:
    check_cuda_availability()
except Exception as e:
    logger.error(f"Fatal error: {e}")
    exit(1)

# ================== GLOBALS ==================

server_address = os.getenv("SERVER_ADDRESS", "127.0.0.1")
client_id = str(uuid.uuid4())

# ComfyUI input directory (IMPORTANT)
COMFY_INPUT_DIR = "/workspace/ComfyUI/input"

# ================== HELPERS ==================

def save_base64_image_to_comfyui(data_base64: str, filename: str) -> str:
    """
    Decode a base64 image and save it into ComfyUI/input.
    This is required for LoadImage node to work.
    """
    try:
        decoded = base64.b64decode(data_base64)
        os.makedirs(COMFY_INPUT_DIR, exist_ok=True)
        file_path = os.path.join(COMFY_INPUT_DIR, filename)
        with open(file_path, "wb") as f:
            f.write(decoded)
        logger.info(f"Saved image to ComfyUI input: {file_path}")
        return file_path
    except (binascii.Error, ValueError) as e:
        raise ValueError("Invalid base64 image input") from e


def queue_prompt(prompt):
    url = f"http://{server_address}:8188/prompt"
    payload = {"prompt": prompt, "client_id": client_id}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data)
    return json.loads(urllib.request.urlopen(req).read())


def get_image(filename, subfolder, folder_type):
    url = f"http://{server_address}:8188/view"
    params = {
        "filename": filename,
        "subfolder": subfolder,
        "type": folder_type,
    }
    query = urllib.parse.urlencode(params)
    with urllib.request.urlopen(f"{url}?{query}") as response:
        return response.read()


def get_history(prompt_id):
    url = f"http://{server_address}:8188/history/{prompt_id}"
    with urllib.request.urlopen(url) as response:
        return json.loads(response.read())


def get_images(ws, prompt):
    prompt_id = queue_prompt(prompt)["prompt_id"]
    output_images = {}

    while True:
        msg = ws.recv()
        if isinstance(msg, str):
            data = json.loads(msg)
            if data["type"] == "executing":
                if data["data"]["node"] is None and data["data"]["prompt_id"] == prompt_id:
                    break

    history = get_history(prompt_id)[prompt_id]

    for node_id, node_output in history["outputs"].items():
        images_output = []
        if "images" in node_output:
            for image in node_output["images"]:
                image_bytes = get_image(
                    image["filename"],
                    image["subfolder"],
                    image["type"],
                )
                images_output.append(
                    base64.b64encode(image_bytes).decode("utf-8")
                )
        output_images[node_id] = images_output

    return output_images


def get_workflow_from_input(job_input):
    """
    Return a deep-copied workflow provided by the client.
    No modifications are applied.
    """
    workflow_input = job_input.get("workflow")
    if workflow_input is None:
        raise ValueError("Workflow must be provided in job input")

    if isinstance(workflow_input, str):
        workflow = json.loads(workflow_input)
    elif isinstance(workflow_input, dict):
        workflow = workflow_input
    else:
        raise ValueError("Workflow must be a JSON object or string")

    return copy.deepcopy(workflow)

# ================== HANDLER ==================

def handler(job):
    job_input = job.get("input", {})
    logger.info("Received job input")

    # Image is ALWAYS base64
    image_base64 = job_input.get("image_path")
    if not image_base64:
        return {"error": "image_path (base64) is required"}

    # Save image directly into ComfyUI/input
    save_base64_image_to_comfyui(
        image_base64,
        filename="input_image.png"
    )

    try:
        workflow = get_workflow_from_input(job_input)
    except Exception as e:
        logger.error(str(e))
        return {"error": str(e)}

    # NOTE:
    # Workflow is used AS-IS.
    # No node overrides, no parameter patching.

    ws_url = f"ws://{server_address}:8188/ws?clientId={client_id}"

    # Wait for ComfyUI HTTP to be ready
    http_url = f"http://{server_address}:8188/"
    for _ in range(180):
        try:
            urllib.request.urlopen(http_url, timeout=5)
            break
        except Exception:
            import time
            time.sleep(1)
    else:
        return {"error": "ComfyUI server is not reachable"}

    ws = websocket.WebSocket()
    ws.connect(ws_url)

    images = get_images(ws, workflow)
    ws.close()

    if not images:
        return {"error": "No images generated"}

    for node_id in images:
        if images[node_id]:
            return {"image": images[node_id][0]}

    return {"error": "Image output not found"}

# ================== START ==================

runpod.serverless.start({"handler": handler})
