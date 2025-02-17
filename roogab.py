#Requirements: pip install flask python-socketio beautifulsoup4
import os
import time
import threading
import re
import logging
import socketio
from collections import deque
from typing import Optional, Dict, Tuple
from flask import Flask, request, jsonify
from bs4 import BeautifulSoup

# Configuration
API_KEY = os.environ.get("API_KEY", "my-secret-key")
NEWCHAT_TIMEOUT = 10  # Timeout for new chat creation
RESPONSE_TIMEOUT = 120  # Timeout for response generation
QUEUE_TIMEOUT = 180  # Timeout for queue processing
SEND_DELAY = 0.4  # Delay after deleting conversations
SERVER_PORT = 5000

# Logging configuration
ENABLE_FILE_LOGGING = False  # Set to True to enable logging to file
LOG_FILE = "gabchat_debug.log"

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
if ENABLE_FILE_LOGGING:
    fh = logging.FileHandler(LOG_FILE)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(formatter)
    logger.addHandler(fh)
ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
ch.setFormatter(formatter)
logger.addHandler(ch)

app = Flask(__name__)

# --- Begin AGENTS Dictionary ---
AGENTS = {
    "Arya": "65a2deb8cf4d2ca0d212a988",
    "Gemini 2.0 Flash": "67a40544f0b1af2d15c142b1",
    "ChatGPT o3-mini": "679d295927b65ffc6a17887c",
    "Perplexity Sonar": "679ae33f27b65ffc6a54e717",
    "Perplexity": "67906e27568b23926645baad",
    "DeepSeek R1": "678eabb0dbc13bd3c4a66b91",
    "DeepSeek V3": "678eab60dbc13bd3c4a6048a",
    "ChatGPT o1-mini": "678af1727b3bb83458ef9a6d",
    "ChatGPT 4o": "678af1077b3bb83458ef00c9",
    "Claude Haiku": "678aefe22510422feba4a3a7",
    "Claude Sonnet": "678aef722510422feba40677",
    "Reasoning": "677e1f785e3bd0a24b0e796d"
}
# --- End AGENTS Dictionary ---

# --- Begin GabChat Classes ---
class Conversation:
    def __init__(self, id: str, parent: 'GabChat'):
        self.id = id
        self._parent = parent
        self.is_generating = True
        self.response_complete = threading.Event()
        self.response_complete.clear()
        self.full_response = ""
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0

    def handle_token_stream(self, data: dict):
        self.full_response = data.get('content','')

    def handle_stream_end(self, data: dict):
        self.is_generating = False
        self.response_complete.set()
        self.total_completion_tokens += len(self.full_response.split())

    def send_message(self, prompt: str) -> Optional[str]:
        if self.is_generating:
            if self._parent.verbose:
                logger.info(f"Conversation {self.id} is already generating")
            return None
        self.is_generating = True
        self.response_complete.clear()
        self.full_response = ""
        try:
            self.total_prompt_tokens += len(prompt.split())
            self._parent.sio.emit('sendPrompt', {
                'prompt': prompt,
                'c': self.id
            })
            if not self.response_complete.wait(timeout=RESPONSE_TIMEOUT):
                raise TimeoutError(f"Timed out waiting for response after {RESPONSE_TIMEOUT}s. Conversation state: is_generating={self.is_generating}")
            return self.full_response
        except Exception as e:
            if self._parent.verbose:
                logger.exception(f"Error sending message: {e}")
            self.is_generating = False
            return None

    def delete(self, send_delay: float = SEND_DELAY) -> bool:
        """Delete this conversation."""
        try:
            self._parent.sio.emit('deleteConversation', {'c': self.id})
            if self.id in self._parent.conversations:
                del self._parent.conversations[self.id]
            # Clean up queues and locks
            if self.id in conversation_queues:
                del conversation_queues[self.id]
            if self.id in conversation_locks:
                del conversation_locks[self.id]
            time.sleep(send_delay)
            return True
        except Exception as e:
            if self._parent.verbose:
                logger.exception(f"Error deleting conversation: {e}")
            return False

class GabChat:
    def __init__(self, cookies: Optional[Dict[str, str]] = None, verbose: bool = False,
                 wait_timeout: int = NEWCHAT_TIMEOUT,
                 response_timeout: int = RESPONSE_TIMEOUT):
        self.sio = socketio.Client(engineio_logger=verbose, logger=verbose)
        self.verbose = verbose
        self.cookies = cookies or {}
        self.conversations: Dict[str, Conversation] = {}
        self.pending_conversation = None
        self.new_conversation_event = threading.Event()
        self.wait_timeout = wait_timeout
        self.response_timeout = response_timeout
        self._setup_socket_handlers()

    def _setup_socket_handlers(self):
        @self.sio.event
        def connect():
            if self.verbose:
                logger.info("[Connected to GabChat server]")

        @self.sio.event
        def disconnect():
            if self.verbose:
                logger.info("[Disconnected from GabChat server]")
            # Reset all global conversation state
            conversation_queues.clear()
            conversation_locks.clear()
            ip_conversations.clear()
            # Attempt to reconnect
            self._reconnect()

        @self.sio.on('loadConversation')
        def on_load_conversation(data):
            if self.verbose:
                logger.info(f"[Got conversation ID: {data.get('id')}]")
            conv_id = data.get('id')
            if conv_id:
                conversation = Conversation(
                    id=conv_id,
                    parent=self
                )
                self.conversations[conv_id] = conversation
                self.sio.emit('subscribe', [f'c:{conv_id}'])
                self.pending_conversation = conversation
                self.new_conversation_event.set()

        @self.sio.on('tokenStream')
        def on_token_stream(data):
            if conv := self.conversations.get(data.get('c')):
                conv.handle_token_stream(data)

        @self.sio.on('streamEnd')
        def on_stream_end(data):
            if conv := self.conversations.get(data.get('c')):
                conv.handle_stream_end(data)

        @self.sio.on("*")
        def on_handle_all(event, data):
            if self.verbose and event not in ['tokenStream', 'streamEnd', 'loadConversation']:
                logger.debug('Caught unhandled event %s: %s', event, data)

    def _reconnect(self):
        """Attempt to reconnect with exponential backoff."""
        max_attempts = 5
        base_delay = 1  # Start with 1 second delay
        attempt = 0
        while attempt < max_attempts:
            delay = base_delay * (2 ** attempt)  # Exponential backoff
            if self.verbose:
                logger.info(f"[Attempting to reconnect in {delay} seconds (attempt {attempt + 1}/{max_attempts})]")
            time.sleep(delay)
            try:
                if self.connect():
                    if self.verbose:
                        logger.info("[Successfully reconnected to GabChat server]")
                    return True
            except Exception as e:
                if self.verbose:
                    logger.error(f"[Reconnection attempt {attempt + 1} failed: {e}]")
            attempt += 1
        if self.verbose:
            logger.error("[Failed to reconnect after maximum attempts]")
        return False

    def connect(self) -> bool:
        """Connect to the GabChat server."""
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:135.0) Gecko/20100101 Firefox/135.0",
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.5",
                "Accept-Encoding": "gzip, deflate, br, zstd",
                "Origin": "https://gab.ai",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
                "Sec-GPC": "1",
                "Pragma": "no-cache",
                "Cache-Control": "no-cache",
            }
            if self.cookies:
                headers["Cookie"] = "; ".join([f"{k}={v}" for k, v in self.cookies.items()])
            self.sio.connect(
                'https://gab.ai/socket.io',
                headers=headers,
                transports=['websocket'],
                wait_timeout=self.wait_timeout
            )
            return True
        except Exception as e:
            if self.verbose:
                logger.exception(f"Connection error: {e}")
            return False

    def new_conversation(self, prompt: str, agent_id: str, timeout: Optional[int] = None) -> Tuple[Optional[Conversation], Optional[str]]:
        """
        Create a new conversation and return (Conversation, initial_response).
        The agent_id is still passed along according to the GabChat protocol.
        
        Args:
            prompt: The initial message to send
            agent_id: The ID of the agent to use
            timeout: Optional custom timeout, otherwise uses self.response_timeout
        """
        self.new_conversation_event.clear()
        self.pending_conversation = None
        self.sio.emit('newConversation', {
            'prompt': prompt,
            'agentId': agent_id
        })
        if self.new_conversation_event.wait(timeout=self.wait_timeout):
            if self.pending_conversation:
                conversation = self.pending_conversation
                conversation.is_generating = True
                conversation.response_complete.clear()
                conversation.total_prompt_tokens += len(prompt.split())
                response_timeout = timeout if timeout is not None else self.response_timeout
                if conversation.response_complete.wait(timeout=response_timeout):
                    return conversation, conversation.full_response
        return None, None

    def disconnect(self):
        try:
            self.sio.disconnect()
            if self.verbose:
                logger.info("[Disconnected from GabChat server]")
        except Exception as e:
            if self.verbose:
                logger.exception("Error during disconnect")

# Instantiate chat client.
chat_client = GabChat(verbose=False)
if not chat_client.connect():
    raise RuntimeError("Failed to connect to GabChat service")
# --- End GabChat Classes ---
# Global conversation mapping by client IP.
conversation_queues = {}
conversation_locks = {}
ip_conversations = {}

# --- Helper Functions ---
def create_error_response(message: str, code: str, conversation_id: Optional[str] = None) -> dict:
    """Create a standardized error response"""
    return {
        "error": message,
        "code": code,
        "conversation_id": conversation_id
    }

def require_api_key(req):
    auth_header = req.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return jsonify(create_error_response(
            "Unauthorized: API key missing",
            "missing_api_key"
        )), 401
    key = auth_header.split("Bearer ")[-1].strip()
    if key != API_KEY:
        return jsonify(create_error_response(
            "Unauthorized: Invalid API key",
            "invalid_api_key"
        )), 401

def build_prompt_from_messages(messages: list) -> str:
    logger.debug("Building prompt from %d message(s).", len(messages))
    system_instruction = \
"""ALWAYS RESPOND WITH THIS EXACT STRUCTURE (MAKE IT A VALID MARKDOWN CODEBLOCK!!):
```lua
print([====[
{{YOUR COMPLETE RESPONSE GOES HERE - NO MARKDOWN/MARKUP}}
]====])
```
Critical requirements (ELSE THE SYSTEM CRASHES FROM PARSER ERROR):
1. Entire response must be wrapped with [====[ and ]====]
2. Use ONLY Lua long-string brackets
3. Do not add any comments or extra text outside the print statement
4. Replace any inner triple backticks with @F_BLOCK@
5. Replace any inner quad equal signs with @E_QUAD@"""
    prompt_parts = [system_instruction]
    # If exactly two messages, treat the first as system-level instructions.
    if len(messages) == 2:
        roo_code = messages.pop(0)
        prompt_parts.append(roo_code['content'])
    # Append content from the latest message; it may be a list or string.
    latest_message = messages[-1]
    if isinstance(latest_message['content'], list):
        for part in latest_message['content']:
            if part['type'] == 'text':
                prompt_parts.append(part['text'])
            else:
                prompt_parts.append(f'Unable to send content of type {part["type"]}')
    else:
        prompt_parts.append(latest_message['content'])
    built_prompt = "\n".join(prompt_parts)
    logger.debug("Built prompt of length: %d", len(built_prompt))
    # For intense debugging:
    # logger.debug("Built prompt: %s", built_prompt)
    return built_prompt

def process_gab_response(html_content: str) -> str:
    """
    Extract and process the Lua print block from the GabChat HTML response.
    """
    soup = BeautifulSoup(html_content, 'html.parser')
    code_block = soup.find('code', class_='language-lua')
    if not code_block:
        raise ValueError("Non-compliant response: No Lua code found")
    decoded_content = code_block.get_text()
    lua_pattern = re.compile(r'print\(\s*(\[=*\[)\s*(.*?)\s*(\]=*\])\s*\)', re.DOTALL)
    match = lua_pattern.search(decoded_content)
    if not match:
        raise ValueError("Invalid Lua print structure")
    open_delim, content, close_delim = match.groups()
    if open_delim.count('=') != close_delim.count('='):
        raise ValueError("Mismatched Lua delimiters during parsing.")
    return content.replace('@F_BLOCK@','```').replace('@E_QUAD@','====')

def process_message_for_ip(messages: list, agent_id: str, client_ip: str, timeout: int = RESPONSE_TIMEOUT) -> dict:
    global ip_conversations
    if not messages:
        logger.error("No messages provided in the prompt.")
        return create_error_response("No messages provided.", "missing_messages")
    prompt = build_prompt_from_messages(messages)
    try:
        if len(messages) == 2 and client_ip in ip_conversations:
            logger.debug("New task detected for IP %s; deleting old conversation", client_ip)
            old_conv = ip_conversations[client_ip]
            old_conv.delete()
            del ip_conversations[client_ip]
            
        if client_ip not in ip_conversations:
            logger.debug("No conversation for IP %s; creating new conversation", client_ip)
            conv, initial_resp = chat_client.new_conversation(prompt, agent_id, timeout=timeout)
            ip_conversations[client_ip] = conv
            full_response = initial_resp
        else:
            conv = ip_conversations[client_ip]
            logger.debug("Found existing conversation for IP %s (ID: %s); sending new message.", client_ip, conv.id)
            full_response = conv.send_message(prompt)
    except Exception as e:
        logger.exception("Error processing message for IP conversation")
        return create_error_response(
            str(e),
            "conversation_error",
            conv.id if 'conv' in locals() else None
        )

    if not full_response or not full_response.strip():
        logger.error("No response received from GabChat.")
        return create_error_response(
            "No response received from GabChat.",
            "empty_response",
            conv.id if 'conv' in locals() else None
        )

    try:
        processed_response = process_gab_response(full_response)
    except Exception as e:
        logger.exception("Error processing GabChat response")
        return create_error_response(
            str(e),
            "processing_error",
            conv.id
        )
    
    response_data = {
        "id": conv.id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": agent_id,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": processed_response
            },
            "finish_reason": "stop"
        }],
    }
    usage = {
        "prompt_tokens": conv.total_prompt_tokens,
        "completion_tokens": conv.total_completion_tokens,
        "total_tokens": conv.total_prompt_tokens + conv.total_completion_tokens
    }
    response_data["usage"] = usage
    return response_data

def queue_conversation_task(conversation_id: str, task_func, *args, **kwargs):
    if conversation_id not in conversation_queues:
        conversation_queues[conversation_id] = deque()
    if conversation_id not in conversation_locks:
        conversation_locks[conversation_id] = threading.Lock()
    result_holder = {}

    def task_wrapper():
        try:
            result_holder["result"] = task_func(*args, **kwargs)
        except Exception as e:
            logger.exception("Exception executing task for conversation %s", conversation_id)
            result_holder["result"] = {"error": str(e)}

    conversation_queues[conversation_id].append(task_wrapper)
    logger.debug("Task enqueued for conversation %s; queue length: %d", conversation_id, len(conversation_queues[conversation_id]))

    def process_queue():
        with conversation_locks[conversation_id]:
            while conversation_queues[conversation_id]:
                current_task = conversation_queues[conversation_id].popleft()
                logger.debug("Processing task for conversation %s", conversation_id)
                current_task()
    threading.Thread(target=process_queue).start()

    start_time = time.time()
    while "result" not in result_holder and (time.time() - start_time) < QUEUE_TIMEOUT:
        time.sleep(0.5)  # More responsive checking

    if "result" not in result_holder:
        error_msg = f"Task timeout after {QUEUE_TIMEOUT}s for conversation {conversation_id}"
        logger.error(error_msg)
        return {
            "error": error_msg,
            "code": "timeout_error",
            "conversation_id": conversation_id,
            "queue_length": len(conversation_queues[conversation_id])
        }
    return result_holder["result"]

# --- Flask Endpoints ---
@app.route("/v1/chat/completions", methods=["POST"])
def chat_completions():
    logger.debug("Received API call at /v1/chat/completions")
    # Uncomment the next line to enforce the API key.
    # require_api_key(request)
    req_data = request.get_json()
    if not req_data:
        return jsonify(create_error_response(
            "Invalid JSON payload",
            "invalid_json"
        )), 400

    # Streaming is not supported.
    stream = req_data.get("stream", False)
    if stream:
        return jsonify(create_error_response(
            "Streaming is disabled in this version.",
            "streaming_disabled"
        )), 400

    model_name = req_data.get("model")
    messages = req_data.get("messages")
    if not model_name or not messages or not isinstance(messages, list):
        logger.error("Invalid request parameters: %s", req_data)
        return jsonify(create_error_response(
            "Payload must include 'model' and a list of 'messages'",
            "invalid_parameters"
        )), 400

    agent_id = AGENTS.get(model_name)
    if not agent_id:
        available_models = ", ".join(AGENTS.keys())
        logger.error("Model not found: %s", model_name)
        return jsonify(create_error_response(
            f"Model not found. Available models: {available_models}",
            "model_not_found"
        )), 400

    client_ip = request.remote_addr
    logger.debug("Using client IP %s as the conversation key.", client_ip)
    result_data = queue_conversation_task(client_ip, process_message_for_ip, messages, agent_id, client_ip)
    if "error" in result_data:
        logger.error("Error processing message: %s", result_data["error"])
        return jsonify(result_data), 400

    logger.debug("Constructed response data: %s", result_data)
    return jsonify(result_data)

@app.route("/v1/models")
def models():
    """List available models"""
    return jsonify({
        "data": [
            {
                "id": model_name,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "gab"
            }
            for model_name in AGENTS.keys()
        ]
    })

@app.route("/")
def index():
    return "GabChat Local Server running. Use /v1/chat/completions endpoint to POST messages."

def run_server():
    logger.info(f"Starting GabChat Local Server on port {SERVER_PORT}")
    try:
        app.run(host="0.0.0.0", port=SERVER_PORT, debug=False, threaded=True)
    except Exception as e:
        logger.exception("Error running the server")
    finally:
        logger.info("Shutting down GabChat Local Server")

if __name__ == "__main__":
    run_server()
