# RooGab

RooGab is a local proxy server that enables the use of various AI models through the Roo Code/Cline extension—though weaker responses than expected. It implements a ChatGPT-compatible API interface, making it seamless to use with tools designed for OpenAI's API.

## Disclaimer

This project is provided for **educational purposes only**. The author is not responsible for how users choose to use this software. Users are responsible for ensuring their use complies with all applicable terms of service and laws. This creates an anonymous socket with the provider but it is possible to provide authentication cookies if desired. Do not expect this to work long.

## Features

- Compatible with the Roo Code/Cline extension
- Supports multiple AI models including Claude variants
- Implements standard chat completion endpoints
- Handles conversation management and token tracking
- Includes robust error handling and reconnection logic

## Available Models

- Claude Haiku
- Claude Sonnet (ONLY TESTED WITH THIS)
- ChatGPT variants
- Gemini 2.0 Flash
- DeepSeek models
- Perplexity models
- And more

## Limitations
(Late edit: If you receive a "**Non-compliant response**", this means the model didn't comply with the prompt required for responses to be parsed. You should be able to just regenerate or start a new task.)

### Technical Constraints
- One conversation per IP address per task
- Large prompts may fail or timeout
- Connection interruptions will lose all current tasks and conversation state
- No persistent storage - all data is kept in memory
- No streaming support
- Rate limiting depends on the underlying service

### Connection Issues
- If the client gets disconnected, all current tasks will be lost
- Reconnection attempts use exponential backoff
- No automatic state recovery after disconnection

### Response Handling
- Responses must follow a specific Lua-compatible format
- Some responses may fail if they don't match the expected structure
- Token counting is approximate based on word count

## Setup

1. Install the required Python packages:
```bash
pip install flask python-socketio beautifulsoup4
```

2. Run the server:
```bash
python roogab.py
```

The server will start on port 5000 by default.

## Usage with Roo Code/Cline

1. Configure your extension to use the local endpoint:
   - API URL: `http://localhost:5000`
   - API Key: `my-secret-key` (configurable in the code)

2. Select any of the available models from the model list

## Configuration

The server configuration can be modified by updating the values in the `Config` class:

```python
@dataclass
class Config:
    API_KEY: str = "my-secret-key"
    NEWCHAT_TIMEOUT: int = 10
    RESPONSE_TIMEOUT: int = 120
    SERVER_PORT: int = 5000
    # ... other settings
```

## API Endpoints

- `/v1/chat/completions` - Main chat completion endpoint
- `/v1/models` - List available models
- `/` - Server status check

## Technical Notes

- The server maintains conversation state by client IP
- Each new task creates a new conversation, replacing any existing one for that IP
- Responses are formatted in a specific Lua-compatible structure
- API key authentication can be enabled/disabled in the code
- No persistent storage - all conversations and state are held in memory
- Server restarts will clear all active conversations

## Educational Purpose

This project is intended as an educational tool for understanding:
- API proxy implementation
- WebSocket communication
- Conversation state management
- Error handling in distributed systems
- Client-server architecture

The user assumes all responsibility for how they choose to use this software.
