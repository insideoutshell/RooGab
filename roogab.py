#!/usr/bin/env python3
#Requirements: pip install flask python-socketio beautifulsoup4
import os
import time
import threading
import re
import logging
import socketio
from collections import deque
from typing import Optional, Dict, Tuple, Any, List
from flask import Flask, request, jsonify
from bs4 import BeautifulSoup
from dataclasses import dataclass

# Configuration with hardcoded values
@dataclass
class Config:
    API_KEY: str = "my-secret-key"
    NEWCHAT_TIMEOUT: int = 10
    RESPONSE_TIMEOUT: int = 120
    QUEUE_TIMEOUT: int = 180
    SEND_DELAY: float = 0.4
    SERVER_PORT: int = 5000
    ENABLE_FILE_LOGGING: bool = False
    LOG_FILE: str = "RooGab_debug.log"
    MAX_RECONNECT_ATTEMPTS: int = 5
    BASE_RECONNECT_DELAY: int = 1

config = Config()

# Logging configuration
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")

if config.ENABLE_FILE_LOGGING:
    fh = logging.FileHandler(config.LOG_FILE)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(formatter)
    logger.addHandler(fh)

ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
ch.setFormatter(formatter)
logger.addHandler(ch)

app = Flask(__name__)

# Custom exceptions
class RooGabError(Exception):
    """Base exception for RooGab errors"""
    pass

class ConnectionError(RooGabError):
    """Raised when there are connection issues"""
    pass

class TimeoutError(RooGabError):
    """Raised when operations timeout"""
    pass

class ResponseError(RooGabError):
    """Raised when there are issues with the response"""
    pass

# Agent configuration
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

class Conversation:
    """Manages individual chat conversations"""
    def __init__(self, id: str, parent: 'RooGab'):
        """
        Initialize a new conversation.
        
        Args:
            id: Unique conversation identifier
            parent: Reference to parent RooGab instance
        """
        self.id = id
        self._parent = parent
        self.is_generating = True
        self.response_complete = threading.Event()
        self.response_complete.clear()
        self.full_response = ""
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0

    def handle_token_stream(self, data: dict) -> None:
        """Handle incoming token stream data"""
        self.full_response = data.get('content', '')

    def handle_stream_end(self, data: dict) -> None:
        """Handle stream end event"""
        self.is_generating = False
        self.response_complete.set()
        self.total_completion_tokens += len(self.full_response.split())

    def send_message(self, prompt: str) -> Optional[str]:
        """
        Send a message in this conversation.
        
        Args:
            prompt: The message to send
            
        Returns:
            The response string or None if there was an error
            
        Raises:
            TimeoutError: If response generation times out
        """
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
            
            if not self.response_complete.wait(timeout=config.RESPONSE_TIMEOUT):
                raise TimeoutError(
                    f"Timed out waiting for response after {config.RESPONSE_TIMEOUT}s. "
                    f"Conversation state: is_generating={self.is_generating}"
                )
            return self.full_response

        except Exception as e:
            if self._parent.verbose:
                logger.exception(f"Error sending message: {e}")
            self.is_generating = False
            return None

    def delete(self, send_delay: float = config.SEND_DELAY) -> bool:
        """
        Delete this conversation.
        
        Args:
            send_delay: Time to wait after deletion
            
        Returns:
            True if deletion was successful, False otherwise
        """
        try:
            self._parent.sio.emit('deleteConversation', {'c': self.id})
            
            # Clean up conversation state
            if self.id in self._parent.conversations:
                del self._parent.conversations[self.id]
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

class RooGab:
    """Main chat client for interfacing with RooGab server"""
    def __init__(
        self,
        cookies: Optional[Dict[str, str]] = None,
        verbose: bool = False,
        wait_timeout: int = config.NEWCHAT_TIMEOUT,
        response_timeout: int = config.RESPONSE_TIMEOUT
    ):
        """
        Initialize RooGab client.
        
        Args:
            cookies: Optional cookies for authentication
            verbose: Enable verbose logging
            wait_timeout: Timeout for new chat creation
            response_timeout: Timeout for response generation
        """
        self.sio = socketio.Client(engineio_logger=verbose, logger=verbose)
        self.verbose = verbose
        self.cookies = cookies or {}
        self.conversations: Dict[str, Conversation] = {}
        self.pending_conversation = None
        self.new_conversation_event = threading.Event()
        self.wait_timeout = wait_timeout
        self.response_timeout = response_timeout
        self._setup_socket_handlers()

    def _setup_socket_handlers(self) -> None:
        """Configure socket.io event handlers"""
        @self.sio.event
        def connect():
            if self.verbose:
                logger.info("[Connected to RooGab server]")

        @self.sio.event
        def disconnect():
            if self.verbose:
                logger.info("[Disconnected from RooGab server]")
            self._cleanup_state()
            # Attempt to reconnect
            self._reconnect()

        @self.sio.on('loadConversation')
        def on_load_conversation(data: dict):
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
        def on_token_stream(data: dict):
            if conv := self.conversations.get(data.get('c')):
                conv.handle_token_stream(data)

        @self.sio.on('streamEnd')
        def on_stream_end(data: dict):
            if conv := self.conversations.get(data.get('c')):
                conv.handle_stream_end(data)

        @self.sio.on("*")
        def on_handle_all(event: str, data: Any):
            if self.verbose and event not in ['tokenStream', 'streamEnd', 'loadConversation']:
                logger.debug('Caught unhandled event %s: %s', event, data)

    def _cleanup_state(self) -> None:
        """Clean up global conversation state"""
        conversation_queues.clear()
        conversation_locks.clear()
        ip_conversations.clear()

    def _reconnect(self) -> bool:
        """
        Attempt to reconnect with exponential backoff.
        
        Returns:
            True if reconnection successful, False otherwise
        """
        attempt = 0
        while attempt < config.MAX_RECONNECT_ATTEMPTS:
            delay = config.BASE_RECONNECT_DELAY * (2 ** attempt)
            if self.verbose:
                logger.info(
                    f"[Attempting to reconnect in {delay} seconds "
                    f"(attempt {attempt + 1}/{config.MAX_RECONNECT_ATTEMPTS})]"
                )
            time.sleep(delay)
            
            try:
                if self.connect():
                    if self.verbose:
                        logger.info("[Successfully reconnected to RooGab server]")
                    return True
            except Exception as e:
                if self.verbose:
                    logger.error(f"[Reconnection attempt {attempt + 1} failed: {e}]")
            attempt += 1
            
        if self.verbose:
            logger.error("[Failed to reconnect after maximum attempts]")
        return False

    def connect(self) -> bool:
        """
        Connect to the RooGab server.
        
        Returns:
            True if connection successful, False otherwise
            
        Raises:
            ConnectionError: If connection fails
        """
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:135.0) Gecko/20100101 Firefox/135.0",
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.5",
                "Accept-Encoding": "gzip, deflate, br, zstd",
                "Origin": "".join(['htt','ps://ga','b.ai/']),
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
                "".join(['htt','ps://ga','b.ai/so','cket.io']),
                headers=headers,
                transports=['websocket'],
                wait_timeout=self.wait_timeout
            )
            return True
            
        except Exception as e:
            if self.verbose:
                logger.exception(f"Connection error: {e}")
            raise ConnectionError(f"Failed to connect: {str(e)}")

    def new_conversation(
        self,
        prompt: str,
        agent_id: str,
        timeout: Optional[int] = None
    ) -> Tuple[Optional[Conversation], Optional[str]]:
        """
        Create a new conversation and return (Conversation, initial_response).
        
        Args:
            prompt: The initial message to send
            agent_id: The ID of the agent to use
            timeout: Optional custom timeout
            
        Returns:
            Tuple of (Conversation object, initial response string)
            
        Raises:
            TimeoutError: If conversation creation times out
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

    def disconnect(self) -> None:
        """Safely disconnect from the RooGab server"""
        try:
            self.sio.disconnect()
            if self.verbose:
                logger.info("[Disconnected from RooGab server]")
        except Exception as e:
            if self.verbose:
                logger.exception("Error during disconnect")

# Initialize chat client
chat_client = RooGab(verbose=False)
if not chat_client.connect():
    raise RuntimeError("Failed to connect to RooGab service")

# Global state
conversation_queues: Dict[str, deque] = {}
conversation_locks: Dict[str, threading.Lock] = {}
ip_conversations: Dict[str, Conversation] = {}

def create_error_response(
    message: str,
    code: str,
    conversation_id: Optional[str] = None
) -> dict:
    """
    Create a standardized error response.
    
    Args:
        message: Error message
        code: Error code
        conversation_id: Optional conversation ID
        
    Returns:
        Dictionary containing error details
    """
    return {
        "error": message,
        "code": code,
        "conversation_id": conversation_id
    }

def require_api_key(req: request) -> Optional[Tuple[dict, int]]:
    """
    Validate API key from request headers.
    
    Args:
        req: Flask request object
        
    Returns:
        None if valid, or tuple of (error response, status code)
    """
    auth_header = req.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return jsonify(create_error_response(
            "Unauthorized: API key missing",
            "missing_api_key"
        )), 401
        
    key = auth_header.split("Bearer ")[-1].strip()
    if key != config.API_KEY:
        return jsonify(create_error_response(
            "Unauthorized: Invalid API key",
            "invalid_api_key"
        )), 401
    return None

def build_prompt_from_messages(messages: List[dict]) -> str:
    """
    Build a prompt string from a list of messages.
    
    Args:
        messages: List of message dictionaries
        
    Returns:
        Combined prompt string
    """
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

    # If exactly two messages, treat the first as system-level instructions
    if len(messages) == 2:
        roo_code = messages.pop(0)
        prompt_parts.append(roo_code['content'])

    # Append content from the latest message
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
    return built_prompt

def process_gab_response(html_content: str) -> str:
    """
    Extract and process the Lua print block from the RooGab HTML response.
    
    Args:
        html_content: Raw HTML response from RooGab
        
    Returns:
        Processed response string
        
    Raises:
        ValueError: If response format is invalid
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

    return content.replace('@F_BLOCK@', '```').replace('@E_QUAD@', '====')

def process_message_for_ip(
    messages: List[dict],
    agent_id: str,
    client_ip: str,
    timeout: int = config.RESPONSE_TIMEOUT
) -> dict:
    """
    Process a message for a specific IP address.
    
    Args:
        messages: List of message dictionaries
        agent_id: ID of the agent to use
        client_ip: Client's IP address
        timeout: Response timeout in seconds
        
    Returns:
        Response data dictionary
    """
    if not messages:
        logger.error("No messages provided in the prompt.")
        return create_error_response("No messages provided.", "missing_messages")

    prompt = build_prompt_from_messages(messages)
    conv = None

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
            logger.debug("Found existing conversation for IP %s (ID: %s); sending new message.",
                      client_ip, conv.id)
            full_response = conv.send_message(prompt)

    except Exception as e:
        logger.exception("Error processing message for IP conversation")
        return create_error_response(
            str(e),
            "conversation_error",
            conv.id if conv else None
        )

    if not full_response or not full_response.strip():
        logger.error("No response received from RooGab.")
        return create_error_response(
            "No response received from RooGab.",
            "empty_response",
            conv.id if conv else None
        )

    try:
        processed_response = process_gab_response(full_response)
    except Exception as e:
        logger.exception("Error processing RooGab response")
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

def queue_conversation_task(conversation_id: str, task_func: callable, *args, **kwargs) -> Any:
    """
    Queue a conversation task for execution.
    
    Args:
        conversation_id: Unique conversation identifier
        task_func: Function to execute
        *args: Positional arguments for task_func
        **kwargs: Keyword arguments for task_func
        
    Returns:
        Result from task_func execution
    """
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
    logger.debug("Task enqueued for conversation %s; queue length: %d",
               conversation_id, len(conversation_queues[conversation_id]))

    def process_queue():
        with conversation_locks[conversation_id]:
            while conversation_queues[conversation_id]:
                current_task = conversation_queues[conversation_id].popleft()
                logger.debug("Processing task for conversation %s", conversation_id)
                current_task()

    threading.Thread(target=process_queue).start()

    start_time = time.time()
    while "result" not in result_holder and (time.time() - start_time) < config.QUEUE_TIMEOUT:
        time.sleep(0.5)

    if "result" not in result_holder:
        error_msg = f"Task timeout after {config.QUEUE_TIMEOUT}s for conversation {conversation_id}"
        logger.error(error_msg)
        return {
            "error": error_msg,
            "code": "timeout_error",
            "conversation_id": conversation_id,
            "queue_length": len(conversation_queues[conversation_id])
        }
    return result_holder["result"]

# Flask Routes
@app.route("/v1/chat/completions", methods=["POST"])
def chat_completions():
    """Handle chat completion requests"""
    logger.debug("Received API call at /v1/chat/completions")
    
    # Enable API key validation
    if error_response := require_api_key(request):
        return error_response

    req_data = request.get_json()
    if not req_data:
        return jsonify(create_error_response(
            "Invalid JSON payload",
            "invalid_json"
        )), 400

    # Check for unsupported streaming
    if req_data.get("stream", False):
        return jsonify(create_error_response(
            "Streaming is disabled in this version.",
            "streaming_disabled"
        )), 400

    # Validate required parameters
    model_name = req_data.get("model")
    messages = req_data.get("messages")
    if not model_name or not messages or not isinstance(messages, list):
        logger.error("Invalid request parameters: %s", req_data)
        return jsonify(create_error_response(
            "Payload must include 'model' and a list of 'messages'",
            "invalid_parameters"
        )), 400

    # Validate model availability
    agent_id = AGENTS.get(model_name)
    if not agent_id:
        available_models = ", ".join(AGENTS.keys())
        logger.error("Model not found: %s", model_name)
        return jsonify(create_error_response(
            f"Model not found. Available models: {available_models}",
            "model_not_found"
        )), 400

    # Process the request
    client_ip = request.remote_addr
    logger.debug("Using client IP %s as the conversation key.", client_ip)
    result_data = queue_conversation_task(client_ip, process_message_for_ip,
                                       messages, agent_id, client_ip)

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
    """Root endpoint"""
    return "RooGab Local Server running. Use /v1/chat/completions endpoint to POST messages."

def run_server() -> None:
    """Start the Flask server"""
    logger.info(f"Starting RooGab Local Server on port {config.SERVER_PORT}")
    try:
        app.run(
            host="0.0.0.0",
            port=config.SERVER_PORT,
            debug=False,
            threaded=True
        )
    except Exception as e:
        logger.exception("Error running the server")
    finally:
        logger.info("Shutting down RooGab Local Server")
        chat_client.disconnect()

if __name__ == "__main__":
    run_server()
