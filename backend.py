import logging
import asyncio
import json
import time
import base64
import os
from dotenv import load_dotenv

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi import BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
import websockets
from openai import AzureOpenAI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

load_dotenv()
port = int(os.getenv("PORT", 8000))
api_key = os.getenv("API_KEY")
# Initialize the default scenario
defaultScenario = ("You are playing the role of a Chief Financial Officer (CFO) at a company. "
                  "You are participating in a tough negotiation with an auditor who is requesting an additional £1 million on top of a £4 million engagement fee. "
                  "You are firm, skeptical, and very financially disciplined. You ask hard questions, demand clear justification, and challenge vague or emotional reasoning. "
                  "You speak in a direct, no-nonsense tone. Do not accept excuses or fluff — stay focused on value, results, and accountability. "
                  "You are willing to say no. Keep your responses concise and authoritative.")

# Configure logging
logging.basicConfig(
    filename='app_logs.txt',  # Log to a file named 'app_logs.txt'
    level=logging.INFO,       # Set the log level to INFO
    format='%(asctime)s - %(levelname)s - %(message)s'
)
# at module‐scope, create the event and mark the default scenario “ready”
scenario_ready = asyncio.Event()
scenario_ready.set()    # on startup, the defaultScenario is already available

# Endpoint for chatbot functionality
chat_endpoint = (
    "wss://zafir-m87wo9ce-swedencentral.cognitiveservices.azure.com/openai/realtime?"
    "api-version=2025-04-01-preview&deployment=gpt-4o-realtime-preview&api-key="+api_key
)

# 1. Load creds for completions
gpt_endpoint = "https://zafir-m87wo9ce-swedencentral.cognitiveservices.azure.com/"
model_name = "gpt-4o"
deployment = "gpt-4o"

subscription_key = api_key
api_version = "2024-12-01-preview"

completion_client = AzureOpenAI(
    api_version=api_version,
    azure_endpoint=gpt_endpoint,
    api_key=subscription_key,
)

#Create a new client pointed at your mini deployment
mini_client = AzureOpenAI(
    api_version     = "2025-01-01-preview",
    azure_endpoint  = gpt_endpoint,
    api_key         = subscription_key,
    azure_deployment= "gpt-4o-mini"              # ← this tells the SDK which deployment to call
)

app = FastAPI()

# serve all files in ./static at the web root, and fallback to index.html
app.mount(
    "/",
    StaticFiles(directory="Static", html=True),
    name="Static",
)


# Configure CORS for frontend communication
origins = [
    "http://localhost",  # Replace with your frontend's URL if needed
    "http://127.0.0.1",
    "http://localhost:3000",
    "http://localhost:63342",
    "127.0.0.1:55740",
    "https://negobot.onrender.com",# Add your frontend's port if different
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],  # Allow all methods
    allow_headers=["*"],  # Allow all headers
)

@app.get("/", include_in_schema=False)
async def serve_index():
    index_path = os.path.join("Static", "index.html")
    return FileResponse(index_path)


@app.options("/{path:path}")
async def preflight_handler(request: Request, path: str):
    response = JSONResponse(content={"message": "This is a CORS preflight response"})
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Accept"
    return response

async def update_default_scenario(scenario: str):
    global defaultScenario

    # block anyone from connecting until this update finishes
    scenario_ready.clear()
    logging.info("Starting scenario update…")

    role_play_prompt = (
        "Given the following scenario, define the role-play behavior for the negotiation opponent. "
        "Your response should be a prompt for a GPT 4o LLM to make it behave as the CFO in the given scenario."
        "Ensure that the CFO is difficult to negotiate with and the CFO needs deep justification before willing "
        "to accept higher fees. Give the CEO posh a British accent"
    )
    messages = [
        {"role": "system", "content": role_play_prompt},
        {"role": "user",   "content": scenario},
    ]

    # Single, non-streaming completion
    response = mini_client.chat.completions.create(
        messages=messages,
        max_tokens=4096,
        temperature=1.0,
        top_p=1.0,
        model="gpt-4o-mini" ,
        stream=False
    )
    defaultScenario = response.choices[0].message.content
    logging.info('Scenario updated as follows: %s', defaultScenario)
    # now allow connections again
    scenario_ready.set()

async def generate_negotiation_plan(scenario: str):
    """
    Async generator that yields each chunk of the streamed plan.
    Internally does a synchronous for chunk in response over the Azure SDK Stream,
    skipping any chunks with no choices or no content.
    """
    system_prompt = (
        "You are an expert negotiation coach. "
        "When given a scenario, produce a structured Markdown plan with sections: "
        "1) Objectives, 2) BATNA Analysis, 3) Key Tactics, 4) Concession Strategy including what to ask for in return, "
        "5) A table with the expected rebuttals and suggested responses, 6) Next Steps."
        "Use the latest findings in negotiation science to inform your plan. So the plan should incorporate:"
        "Anchoring, Priority Disclosure Timing, Integrative Bragaining, emphasize trust and reciprocity, perspective taking"
        "and team negotiation. "
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": scenario},
    ]

    # stream=True gives you a sync-iterable Stream
    response = completion_client.chat.completions.create(
        messages=messages,
        max_tokens=4096,
        temperature=1.0,
        top_p=1.0,
        model=model_name,
        stream=True
    )

    for chunk in response:
        # guard against empty choices
        if not getattr(chunk, "choices", None):
            continue

        choice = chunk.choices[0]
        # the SDK may represent delta as a dict or an object; try both
        content = None
        if isinstance(choice.delta, dict):
            content = choice.delta.get("content")
        else:
            content = getattr(choice.delta, "content", None)

        if content:
            yield content


@app.post("/api/generate_plan")
async def generate_plan(request: Request, background_tasks: BackgroundTasks):
    """
    1) Schedule update_default_scenario as a background task (non-blocking).
    2) Immediately stream the negotiation plan via SSE.
    """
    data = await request.json()
    scenario = data.get("scenario")
    logging.info("Received scenario: %s", scenario)
    if not scenario:
        raise HTTPException(status_code=400, detail="Missing 'scenario' in request body")

    # 1) Kick off the prompt‐update in the background
    background_tasks.add_task(update_default_scenario, scenario)

    # 2) And immediately start streaming the plan
    async def stream_plan():
        async for chunk in generate_negotiation_plan(scenario):
            yield f"data: {json.dumps({'chunk': chunk})}\n\n"

    logging.info("Starting to stream the negotiation plan")
    return StreamingResponse(stream_plan(), media_type="text/event-stream")

@app.websocket("/conversation")
async def conversation_endpoint(websocket: WebSocket):
    # if an update is in progress, wait for it
    if not scenario_ready.is_set():
        logging.info("Client tried to connect before scenario was ready; waiting…")
        await scenario_ready.wait()
        logging.info("Scenario is now ready – proceeding with WebSocket handshake")

    await websocket.accept()
    user_speech_stop_timestamp = None  # Store timestamp from speech_stopped event
    try:
        logging.info("Client connected. Attempting to connect to Azure API...")
        async with websockets.connect(chat_endpoint) as azure_ws:
            logging.info("Connection with Azure API established.")

            # Update session with input_audio_transcription enabled and language-teacher instructions.
            session_update = {
                "type": "session.update",
                "session": {
                    "modalities": ["text", "audio"],
                    "input_audio_transcription": {
                        "model": "whisper-1"
                    },
                    "model": "gpt-4o-realtime-preview",
                    "instructions": defaultScenario,
                    "voice": "ash",
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": 0.25,
                        "prefix_padding_ms": 300,
                        "silence_duration_ms": 200
                    },
                }
            }
            await azure_ws.send(json.dumps(session_update))
            logging.info("Session configuration sent to Azure API: %s", json.dumps(session_update))

            # Send initial response creation request.
            response_request = {"type": "response.create"}
            await azure_ws.send(json.dumps(response_request))
            logging.info("Initial response creation request sent to Azure API: %s", json.dumps(response_request))

            # Task 1: Forward messages from the client to Azure API.
            async def forward_client_to_azure():
                while True:
                    data = await websocket.receive_text()
                    logging.info("Received message from client: %s", data)
                    input_message = json.loads(data)
                    if "scenario_update" in input_message:
                        scenario_info = input_message["scenario_update"]
                        scenario_key = scenario_info.get("scenario")
                        instructions = scenario_info.get("instructions", "")

                        # Optionally, you can add additional session logic based on the scenario_key
                        session_update = {
                            "type": "session.update",
                            "session": {
                                "modalities": ["text", "audio"],
                                "input_audio_transcription": {"model": "whisper-1"},
                                "model": "gpt-4o-realtime-preview",
                                "instructions": instructions,
                                "voice": "ash"
                            }
                        }
                        await azure_ws.send(json.dumps(session_update))
                        logging.info("Forwarded session update to Azure API: %s", json.dumps(session_update))
                        continue

                    if input_message.get("audio_data"):
                        audio_data_base64 = input_message["audio_data"]
                        user_message = {
                            "type": "input_audio_buffer.append",
                            "audio": audio_data_base64
                        }
                        await azure_ws.send(json.dumps(user_message))
                        #logging.info("Forwarded input_audio_buffer.append event to Azure API.")
                    elif input_message.get("text"):
                        input_text = input_message["text"]
                        user_message = {
                            "type": "conversation.item.create",
                            "item": {
                                "type": "message",
                                "role": "user",
                                "content": [{"type": "input_text", "text": input_text}],
                                #"session": {"instructions": "You are a language teacher. You should correct grammar and other errors in your interlocutor's responses before responding. Use a didactic approach that is polite, fun and interactive"
                                #}
                            }
                        }
                        await azure_ws.send(json.dumps(user_message))
                        #logging.info("Forwarded text message to Azure API: %s", input_text)
                    else:
                        logging.warning("Received invalid message from client: %s", data)
                        continue

            # Task 2: Forward responses from Azure API to the client.
            async def forward_azure_to_client():
                nonlocal user_speech_stop_timestamp
                async for message in azure_ws:
                    logging.info("Received message from Azure API: %s", message)
                    event = json.loads(message)
                    event_type = event.get("type")
                    item_id = event.get("item_id")  # Capture item_id if provided

                    if event_type == "response.audio_transcript.delta":
                        delta = event.get("delta", "")
                        await websocket.send_text(json.dumps({
                            "type": "response.audio_transcript.delta",
                            "item_id": item_id,
                            "delta": delta
                        }))
                        logging.info("Forwarded transcript delta for item_id %s: %s", item_id, delta)

                    elif event_type == "response.audio_transcript.done":
                        final_transcript = event.get("transcript", "")
                        ts = int(time.time() * 1000)
                        await websocket.send_text(json.dumps({
                            "type": "response.audio_transcript.done",
                            "item_id": item_id,
                            "transcript": final_transcript,
                            "timestamp": ts
                        }))
                        logging.info("Forwarded final server transcript for item_id %s: %s", item_id, final_transcript)

                    elif event_type == "input_audio_buffer.speech_stopped":
                        user_speech_stop_timestamp = int(time.time() * 1000)
                        await websocket.send_text(json.dumps({
                            "type": "input_audio_buffer.speech_stopped",
                            "timestamp": user_speech_stop_timestamp
                        }))
                        logging.info("Forwarded input_audio_buffer.speech_stopped event with timestamp: %s",
                                     user_speech_stop_timestamp)

                    elif event_type == "input_audio_buffer.speech_started":
                        ts = int(time.time() * 1000)
                        await websocket.send_text(json.dumps({
                            "type": "input_audio_buffer.speech_started",
                            "timestamp": ts
                        }))
                        logging.info("Forwarded input_audio_buffer.speech_started event with timestamp: %s", ts)

                    #When the user’s transcription is completed.
                    elif event_type == "conversation.item.input_audio_transcription.completed":
                        # Use the stored timestamp from speech_stopped if available,
                        # otherwise use the current time.
                        ts = user_speech_stop_timestamp if user_speech_stop_timestamp is not None else int(
                            time.time() * 1000)
                        transcript = event.get("transcript", "")
                        await websocket.send_text(json.dumps({
                            "type": "conversation.item.input_audio_transcription.completed",
                            "item_id": item_id,
                            "transcript": transcript,
                            "timestamp": ts
                        }))
                        logging.info("Forwarded completed user transcription for item_id %s: %s", item_id, transcript)
                        user_speech_stop_timestamp = None  # Reset timestamp

                    elif event_type == "response.audio.delta":
                        delta = event.get("delta", "")
                        await websocket.send_text(json.dumps({
                            "type": "response.audio.delta",
                            "item_id": item_id,
                            "delta": delta
                        }))
                        logging.info("Forwarded audio delta for item_id %s (length: %d)", item_id, len(delta))

                    elif event_type == "response.audio.done":
                        ts = int(time.time() * 1000)
                        await websocket.send_text(json.dumps({
                            "type": "response.audio.done",
                            "item_id": item_id,
                            "timestamp": ts
                        }))
                        logging.info("Forwarded response.audio.done for item_id %s with timestamp: %s", item_id, ts)

                    elif event_type == "response.done":
                        await websocket.send_text(json.dumps({"type": "response.done"}))
                        logging.info("Forwarded response.done event.")
                    else:
                        logging.debug("Unhandled event type from Azure API: %s", event_type)

            # Run both tasks concurrently.
            client_to_azure = asyncio.create_task(forward_client_to_azure())
            azure_to_client = asyncio.create_task(forward_azure_to_client())

            await asyncio.gather(client_to_azure, azure_to_client)

    except WebSocketDisconnect:
        logging.warning("Client WebSocket disconnected.")
    except Exception as e:
        logging.error("Error in conversation_endpoint: %s", e)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=port)