import asyncio
import copy
import json
import os
import logging
import uuid
import httpx
from datetime import datetime, timezone
from prompts import prompts
from quart import (
    Blueprint,
    Quart,
    websocket,
    session,
    jsonify,
    make_response,
    request,
    send_from_directory,
    render_template,
)
from bs4 import BeautifulSoup
from pprint import pprint
import requests
from openai.types.chat import chat_completion
from openai import AsyncAzureOpenAI
from azure.identity.aio import (
    DefaultAzureCredential,
    get_bearer_token_provider
)
from backend.auth.auth_utils import get_authenticated_user_details
from backend.security.ms_defender_utils import get_msdefender_user_json
from backend.history.cosmosdbservice import CosmosConversationClient
from backend.settings import (
    app_settings,
    MINIMUM_SUPPORTED_AZURE_OPENAI_PREVIEW_API_VERSION
)
from backend.utils import (
    format_as_ndjson,
    format_stream_response,
    format_non_streaming_response,
    convert_to_pf_format,
    format_pf_non_streaming_response,
)

bp = Blueprint("routes", __name__, static_folder="static", template_folder="static")

# Status handling
status_message = {}
clients = {}

async def set_status_message(message, page_instance_id):
    await clients[page_instance_id].send(message)

def create_app():
    app = Quart(__name__)
    app.secret_key = os.urandom(24)  # Generates a random secret key
    app.register_blueprint(bp)
    app.config["TEMPLATES_AUTO_RELOAD"] = True

    # Manually add CORS headers to each response
    @app.after_request
    async def after_request(response):
        response.headers.add('Access-Control-Allow-Origin', '*')
        response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
        response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
        return response

    @app.websocket('/ws')
    async def ws():
        page_instance_id = str(uuid.uuid4())
        clients[page_instance_id] = websocket._get_current_object()
        await clients[page_instance_id].send(f"page_instance_id={page_instance_id}")
        try:
            while True:
                message = await websocket.receive()  # Keep the connection open, ignore all messages since we're only sending out              
        except Exception as e:
            print(f"WebSocket exception: {e}")
        finally:
            if page_instance_id in clients:
                del clients[page_instance_id]

    return app

@bp.route("/")
async def index():
    return await render_template(
        "index.html",
        title=app_settings.ui.title,
        favicon=app_settings.ui.favicon
    )

@bp.route("/favicon.ico")
async def favicon():
    return await bp.send_static_file("favicon.ico")

@bp.route("/list-static-files")
async def list_static_files():
    import os
    files = os.listdir(bp.static_folder)
    return {"files": files}

@bp.route("/ms-learn-guy.png")
async def mslearnguy():
    return await send_from_directory(bp.static_folder, "ms-learn-guy.png")

@bp.route("/assets/<path:path>")
async def assets(path):
    return await send_from_directory("static/assets", path)

# Debug settings
DEBUG = os.environ.get("DEBUG", "false")
if DEBUG.lower() == "true":
    logging.basicConfig(level=logging.WARNING) # Make logging.DEBUG to see heavy debugging...
else:
    logging.basicConfig(level=logging.INFO)

logging.getLogger('openai._base_client').setLevel(logging.WARNING)
logging.getLogger('httpcore.connection').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('root').setLevel(logging.WARNING)

USER_AGENT = "GitHubSampleWebApp/AsyncAzureOpenAI/1.0.0"


# Frontend Settings via Environment Variables
frontend_settings = {
    "auth_enabled": app_settings.base_settings.auth_enabled,
    "feedback_enabled": (
        app_settings.chat_history and
        app_settings.chat_history.enable_feedback
    ),
    "ui": {
        "title": app_settings.ui.title,
        "logo": app_settings.ui.logo,
        "chat_logo": app_settings.ui.chat_logo or app_settings.ui.logo,
        "chat_title": app_settings.ui.chat_title,
        "chat_description": app_settings.ui.chat_description,
        "show_share_button": app_settings.ui.show_share_button,
        "show_chat_history_button": app_settings.ui.show_chat_history_button,
    },
    "sanitize_answer": app_settings.base_settings.sanitize_answer,
}


# Enable Microsoft Defender for Cloud Integration
MS_DEFENDER_ENABLED = os.environ.get("MS_DEFENDER_ENABLED", "true").lower() == "true"


# Initialize Azure OpenAI Client
def init_openai_client():
    azure_openai_client = None
    try:
        # API version check
        if (
            app_settings.azure_openai.preview_api_version
            < MINIMUM_SUPPORTED_AZURE_OPENAI_PREVIEW_API_VERSION
        ):
            raise ValueError(
                f"The minimum supported Azure OpenAI preview API version is '{MINIMUM_SUPPORTED_AZURE_OPENAI_PREVIEW_API_VERSION}'"
            )

        # Endpoint
        if (
            not app_settings.azure_openai.endpoint and
            not app_settings.azure_openai.resource
        ):
            raise ValueError(
                "AZURE_OPENAI_ENDPOINT or AZURE_OPENAI_RESOURCE is required"
            )

        endpoint = (
            app_settings.azure_openai.endpoint
            if app_settings.azure_openai.endpoint
            else f"https://{app_settings.azure_openai.resource}.openai.azure.com/"
        )

        # Authentication
        aoai_api_key = app_settings.azure_openai.key
        ad_token_provider = None
        if not aoai_api_key:
            logging.debug("No AZURE_OPENAI_KEY found, using Azure Entra ID auth")
            ad_token_provider = get_bearer_token_provider(
                DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
            )

        # Deployment
        deployment = app_settings.azure_openai.model
        if not deployment:
            raise ValueError("AZURE_OPENAI_MODEL is required")

        # Default Headers
        default_headers = {"x-ms-useragent": USER_AGENT}

        azure_openai_client = AsyncAzureOpenAI(
            api_version=app_settings.azure_openai.preview_api_version,
            api_key=aoai_api_key,
            azure_ad_token_provider=ad_token_provider,
            default_headers=default_headers,
            azure_endpoint=endpoint,
        )

        return azure_openai_client
    except Exception as e:
        logging.exception("Exception in Azure OpenAI initialization", e)
        azure_openai_client = None
        raise e


def init_cosmosdb_client():
    cosmos_conversation_client = None
    if app_settings.chat_history:
        try:
            cosmos_endpoint = (
                f"https://{app_settings.chat_history.account}.documents.azure.com:443/"
            )

            if not app_settings.chat_history.account_key:
                credential = DefaultAzureCredential()
            else:
                credential = app_settings.chat_history.account_key

            cosmos_conversation_client = CosmosConversationClient(
                cosmosdb_endpoint=cosmos_endpoint,
                credential=credential,
                database_name=app_settings.chat_history.database,
                container_name=app_settings.chat_history.conversations_container,
                enable_message_feedback=app_settings.chat_history.enable_feedback,
            )
        except Exception as e:
            logging.exception("Exception in CosmosDB initialization", e)
            cosmos_conversation_client = None
            raise e
    else:
        logging.debug("CosmosDB not configured")

    return cosmos_conversation_client


def prepare_model_args(request_body, request_headers, system_preamble = None, system_prompt = None):
    request_messages = request_body.get("messages", [])
    messages = []
    system_message = system_prompt if system_prompt is not None else (system_preamble + "\n\n" if system_preamble is not None else "") + app_settings.azure_openai.system_message

    if not app_settings.datasource or system_preamble is not None or system_prompt is not None:
        messages = [
            {
                "role": "system",
                "content": system_message
            }
        ]

    for message in request_messages:
        if message:
            messages.append(
                {
                    "role": message["role"],
                    "content": message["content"]
                }
            )

    user_json = None
    if (MS_DEFENDER_ENABLED):
        authenticated_user_details = get_authenticated_user_details(request_headers)
        conversation_id = request_body.get("conversation_id", None)      
        user_json = get_msdefender_user_json(authenticated_user_details, request_headers, conversation_id)

    model_args = {
        "messages": messages,
        "temperature": app_settings.azure_openai.temperature,
        "max_tokens": app_settings.azure_openai.max_tokens,
        "top_p": app_settings.azure_openai.top_p,
        "stop": app_settings.azure_openai.stop_sequence,
        "stream": app_settings.azure_openai.stream,
        "model": app_settings.azure_openai.model,
        "user": user_json
    }

    if app_settings.datasource:
        model_args["extra_body"] = {
            "data_sources": [
                app_settings.datasource.construct_payload_configuration(
                    request=request
                )
            ]
        }

    model_args_clean = copy.deepcopy(model_args)
    if model_args_clean.get("extra_body"):
        secret_params = [
            "key",
            "connection_string",
            "embedding_key",
            "encoded_api_key",
            "api_key",
        ]
        for secret_param in secret_params:
            if model_args_clean["extra_body"]["data_sources"][0]["parameters"].get(
                secret_param
            ):
                model_args_clean["extra_body"]["data_sources"][0]["parameters"][
                    secret_param
                ] = "*****"
        authentication = model_args_clean["extra_body"]["data_sources"][0][
            "parameters"
        ].get("authentication", {})
        for field in authentication:
            if field in secret_params:
                model_args_clean["extra_body"]["data_sources"][0]["parameters"][
                    "authentication"
                ][field] = "*****"
        embeddingDependency = model_args_clean["extra_body"]["data_sources"][0][
            "parameters"
        ].get("embedding_dependency", {})
        if "authentication" in embeddingDependency:
            for field in embeddingDependency["authentication"]:
                if field in secret_params:
                    model_args_clean["extra_body"]["data_sources"][0]["parameters"][
                        "embedding_dependency"
                    ]["authentication"][field] = "*****"

    return model_args


async def promptflow_request(request):
    try:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {app_settings.promptflow.api_key}",
        }
        # Adding timeout for scenarios where response takes longer to come back
        logging.debug(f"Setting timeout to {app_settings.promptflow.response_timeout}")
        async with httpx.AsyncClient(
            timeout=float(app_settings.promptflow.response_timeout)
        ) as client:
            pf_formatted_obj = convert_to_pf_format(
                request,
                app_settings.promptflow.request_field_name,
                app_settings.promptflow.response_field_name
            )
            # NOTE: This only support question and chat_history parameters
            # If you need to add more parameters, you need to modify the request body
            response = await client.post(
                app_settings.promptflow.endpoint,
                json={
                    app_settings.promptflow.request_field_name: pf_formatted_obj[-1]["inputs"][app_settings.promptflow.request_field_name],
                    "chat_history": pf_formatted_obj[:-1],
                },
                headers=headers,
            )
        resp = response.json()
        resp["id"] = request["messages"][-1]["id"]
        return resp
    except Exception as e:
        logging.error(f"An error occurred while making promptflow_request: {e}")


async def send_chat_request(request_body, request_headers, system_preamble = None, system_prompt = None):
    filtered_messages = []
    messages = request_body.get("messages", [])
    for message in messages:
        if message.get("role") != 'tool':
            filtered_messages.append(message)
            
    request_body['messages'] = filtered_messages
    model_args = prepare_model_args(request_body, request_headers, system_preamble, system_prompt)

    try:
        azure_openai_client = init_openai_client()
        raw_response = await azure_openai_client.chat.completions.with_raw_response.create(**model_args)
        response = raw_response.parse()
        
        apim_request_id = raw_response.headers.get("apim-request-id") 
    except Exception as e:
        logging.exception("Exception in send_chat_request")
        raise e

    return response, apim_request_id


async def complete_chat_request(request_body, request_headers):
    if app_settings.base_settings.use_promptflow:
        response = await promptflow_request(request_body)
        history_metadata = request_body.get("history_metadata", {})
        return format_pf_non_streaming_response(
            response,
            history_metadata,
            app_settings.promptflow.response_field_name,
            app_settings.promptflow.citations_field_name
        )
    else:
        response, apim_request_id = await send_chat_request(request_body, request_headers)
        history_metadata = request_body.get("history_metadata", {})
        return format_non_streaming_response(response, history_metadata, apim_request_id)


async def stream_chat_request(request_body, request_headers, system_preamble = None, system_message = None):
    response, apim_request_id = await send_chat_request(request_body, request_headers, system_preamble, system_message)
    history_metadata = request_body.get("history_metadata", {})
    
    async def generate():
        async for completionChunk in response:
            yield format_stream_response(completionChunk, history_metadata, apim_request_id)

    return generate()

def process_raw_response(raw_content):
    # Decode the raw content
    decoded_content = raw_content.decode('utf-8')
    lines = decoded_content.split('\n')
    json_response = [json.loads(line) for line in lines if line.strip()]
    
    # Initialize variables to hold properties and message contents
    combined_content = ""
    final_json = {
        "messages": [],
        "model": None,
        "history_metadata": None,
    }
    
    chat_id = ""
    for obj in json_response:
        try:                    
            # Extract and set top-level properties once
            if final_json["model"] == None:
                final_json["model"] = obj.get("model")
                final_json["history_metadata"] = obj.get("history_metadata")
                chat_id = obj.get("id")
            
            # Extract message contents
            choices = obj.get("choices", [])
            for choice in choices:
                messages = choice.get("messages", [])
                for message in messages:
                    content = message.get("content", "")
                    combined_content += content
        
        except json.JSONDecodeError as e:
            print(f"JSON decode error: {e}")
            continue
        except Exception as e:
            print(f"Error processing object: {e}")
            continue
        
    # Add combined content to the final JSON structure
    final_json["messages"].append({
        "id": chat_id,
        "role": "assistant",
        "content": combined_content,
        "date": datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
    })
    
    return final_json

def concatenate_json_arrays(json_array1, json_array2):
    json_array1 = json_array1.rstrip(']\n\r ')
    json_array2 = json_array2.lstrip('[\n\r ')
    concatenated_json = f"{json_array1}, {json_array2}"
    return concatenated_json

async def search_bing(search):
        # Add your Bing Search V7 subscription key and endpoint to your environment variables.
        subscription_key = app_settings.bing.key
        endpoint = app_settings.bing.endpoint + "/v7.0/search"
        mkt = "en-US"
        params = { 'q' : search, 'mkt' : mkt }
        headers = { 'Ocp-Apim-Subscription-Key' : subscription_key }
        ## Call the API
        try:
            response = requests.get(endpoint, headers=headers, params=params)
            response.raise_for_status()
            search_results = response.json().get("webPages", {}).get("value", [])
            return search_results
        except Exception as e:
            print(f"Error: {e}")
            return None
        
async def send_private_chat(request_body, request_headers, system_preamble = None, system_message = None):
        bg_request_body = copy.deepcopy(request_body)
        bg_request_headers = copy.deepcopy(request_headers)
        bg_request_body["history_metadata"] = None
        bg_request_body["conversation_id"] = str(uuid.uuid4())
        bg_request_body["messages"] = bg_request_body["messages"][-1:]
        result = await stream_chat_request(bg_request_body, bg_request_headers, system_preamble, system_message)
        response = await make_response(format_as_ndjson(result))
        response.timeout = None
        response.mimetype = "application/json-lines"
        response_raw = await response.get_data()
        combined_json = process_raw_response(response_raw) 
        return combined_json["messages"][0]["content"]

async def get_search_results(searches):
        allresults = None
        for search in searches:
            results = await search_bing(search);
            if results == None:
                return "Search error."
            else:
                if allresults == None:
                    allresults = results
                else:
                    allresults += results
        # Remove extraneous fields
                proparray = ["dateLastCrawled", "language", "richFacts", "isNavigational", "isFamilyFriendly", "displayUrl", "searchTags", "noCache", "cachedPageUrl", "datePublishedDisplayText", "datePublished", "id", "primaryImageOfPage", "thumbnailUrl"]
                for obj in allresults:
                    for prop in proparray:
                        if prop in obj:
                            del obj[prop]
                return allresults

async def identify_searches(request_body, request_headers, Summaries = None):
        
        if Summaries is None:
            system_preamble = prompts["identify_searches"];
        else:
            system_preamble = prompts["identify_additional_searches"] + json.dumps(Summaries, indent=4) + "\n\nOriginal System Prompt:\n"
        
        searches = await send_private_chat(request_body, request_headers, system_preamble)

        if isinstance(searches, str):
            if searches == "No searches required.": 
                return None
            else:
                if searches[0] != "[":
                    searches = "[" + searches
                if searches[-1] != "]":
                    searches = searches + "]"
                searches = json.loads(searches)
        return searches

async def get_urls_to_browse(request_body, request_headers, searches):
        searchresults = await get_search_results(searches)
        if searchresults == "Search error.":
            return "Search error."
        else:
            strsearchresults = json.dumps(searchresults, indent=4)
            system_preamble = prompts["get_urls_to_browse"] + strsearchresults + "\n\nOriginal System Prompt:\n"
                        
            URLsToBrowse = await send_private_chat(request_body, request_headers, None, system_preamble)
            return URLsToBrowse

async def fetch_and_parse_url(url):
    response = requests.get(url)
    if response.status_code == 200:  # Raise an error for bad status codes
        # Parse the web page
        soup = BeautifulSoup(response.content, 'html.parser')
        # Extract the main content
        paragraphs = soup.find_all('p')
        # Combine the text from the paragraphs
        content = ' '.join(paragraph.get_text() for paragraph in paragraphs)
        return content
    else:
        return None

async def get_article_summaries(request_body, request_headers, URLsToBrowse):
        Summaries = None
        URLsToBrowse = json.loads(URLsToBrowse)
        Pages = None
        
        async def process_url(URL):
            page_content = await fetch_and_parse_url(URL)
            if page_content is not None: 
                system_prompt = (
                    "The Original System Prompt that follows is your primary objective, "
                    "but for this chat you identified the following URL for further research "
                    "to give your answer: " + URL + 
                    ". Your task now is to provide a summary of relevant content on the page "
                    "that will help us address the feedback on the URL provided by the user "
                    "and document current sources. Return nothing except your summary of the "
                    "key points and any important quotes the content on the page in a single string.\n\n"
                    "Page Content:\n\n" + page_content + "\n\nOriginal System Prompt:\n\n"
                )
                summary = await send_private_chat(request_body, request_headers, None, system_prompt)
                summary = json.loads("{\"URL\" : \"" + URL + "\",\n\"summary\" : " + json.dumps(summary) + "}")
                return summary
            return None

        # Create tasks for all URLs
        tasks = [process_url(URL) for URL in URLsToBrowse]
        
        # Run tasks concurrently
        results = await asyncio.gather(*tasks)
        
        # Filter out None results and collect summaries
        Summaries = [summary for summary in results if summary is not None]
        return Summaries

async def is_background_info_sufficient(request_body, request_headers, Summaries):
        strSummaries = json.dumps(Summaries, indent=4)
        system_preamble = prompts["is_background_info_sufficient"] + strSummaries + "\n\nOriginal System Prompt:\n"

        response = await send_private_chat(request_body, request_headers, system_preamble)
        if response == "More information needed.": 
            
            #debug
            print("\n\nMore information was needed, searching again.\n\n")

            return False
        else:
            return True
        
async def search_and_add_background_references(request_body, request_headers):
        NeedsMoreSummaries = True
        Summaries = None

        page_instance_id = request_body["page_instance_id"]

        while NeedsMoreSummaries:
            searches = await identify_searches(request_body, request_headers)
            if searches == None:
                await set_status_message("Generating answer...", page_instance_id)
                return None
            
            await set_status_message("Searching...", page_instance_id)
            URLsToBrowse = await get_urls_to_browse(request_body, request_headers, searches)
            if URLsToBrowse == "Search error.": 
                return "Search error."       

            await set_status_message("Browsing and analyzing...", page_instance_id)
            if (Summaries is None):
                Summaries = await get_article_summaries(request_body, request_headers, URLsToBrowse)
            else:
                newSummaries = await get_article_summaries(request_body, request_headers, URLsToBrowse)
                Summaries += newSummaries
            
            await set_status_message("Double checking sources...", page_instance_id)
            AreWeDone = await is_background_info_sufficient(request_body, request_headers, Summaries)
            if AreWeDone:
                NeedsMoreSummaries = False

        await set_status_message("Generating answer...", page_instance_id)
        return prompts["background_info_preamble"] + json.dumps(Summaries, indent=4) + "\n\nOriginal System Prompt:\n\n"

async def conversation_internal(request_body, request_headers):
    try:
        system_preamble = await search_and_add_background_references(request_body, request_headers)
        if system_preamble != "Search error.":
            result = await stream_chat_request(request_body, request_headers, system_preamble)
        else:
            result = await stream_chat_request(request_body, request_headers, prompts["search_error_preamble"])
        response = await make_response(format_as_ndjson(result))
        response.timeout = None
        response.mimetype = "application/json-lines"
        return response

    except Exception as ex:
        logging.exception(ex)
        if hasattr(ex, "status_code"):
            return jsonify({"error": str(ex)}), ex.status_code
        else:
            return jsonify({"error": str(ex)}), 500


@bp.route("/conversation", methods=["POST"])
async def conversation():
    if not request.is_json:
        return jsonify({"error": "request must be json"}), 415
    request_json = await request.get_json()

    return await conversation_internal(request_json, request.headers)


@bp.route("/frontend_settings", methods=["GET"])
def get_frontend_settings():
    try:
        return jsonify(frontend_settings), 200
    except Exception as e:
        logging.exception("Exception in /frontend_settings")
        return jsonify({"error": str(e)}), 500


## Conversation History API ##
@bp.route("/history/generate", methods=["POST"])
async def add_conversation():
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    ## check request for conversation_id
    request_json = await request.get_json()
    conversation_id = request_json.get("conversation_id", None)

    try:
        # make sure cosmos is configured
        cosmos_conversation_client = init_cosmosdb_client()
        if not cosmos_conversation_client:
            raise Exception("CosmosDB is not configured or not working")

        # check for the conversation_id, if the conversation is not set, we will create a new one
        history_metadata = {}
        if not conversation_id:
            title = await generate_title(request_json["messages"])
            conversation_dict = await cosmos_conversation_client.create_conversation(
                user_id=user_id, title=title
            )
            conversation_id = conversation_dict["id"]
            history_metadata["title"] = title
            history_metadata["date"] = conversation_dict["createdAt"]

        ## Format the incoming message object in the "chat/completions" messages format
        ## then write it to the conversation history in cosmos
        messages = request_json["messages"]
        if len(messages) > 0 and messages[-1]["role"] == "user":
            createdMessageValue = await cosmos_conversation_client.create_message(
                uuid=str(uuid.uuid4()),
                conversation_id=conversation_id,
                user_id=user_id,
                input_message=messages[-1],
            )
            if createdMessageValue == "Conversation not found":
                raise Exception(
                    "Conversation not found for the given conversation ID: "
                    + conversation_id
                    + "."
                )
        else:
            raise Exception("No user message found")

        await cosmos_conversation_client.cosmosdb_client.close()

        # Submit request to Chat Completions for response
        request_body = await request.get_json()
        history_metadata["conversation_id"] = conversation_id
        request_body["history_metadata"] = history_metadata
        return await conversation_internal(request_body, request.headers)

    except Exception as e:
        logging.exception("Exception in /history/generate")
        return jsonify({"error": str(e)}), 500


@bp.route("/history/update", methods=["POST"])
async def update_conversation():
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    ## check request for conversation_id
    request_json = await request.get_json()
    conversation_id = request_json.get("conversation_id", None)

    try:
        # make sure cosmos is configured
        cosmos_conversation_client = init_cosmosdb_client()
        if not cosmos_conversation_client:
            raise Exception("CosmosDB is not configured or not working")

        # check for the conversation_id, if the conversation is not set, we will create a new one
        if not conversation_id:
            raise Exception("No conversation_id found")

        ## Format the incoming message object in the "chat/completions" messages format
        ## then write it to the conversation history in cosmos
        messages = request_json["messages"]
        if len(messages) > 0 and messages[-1]["role"] == "assistant":
            if len(messages) > 1 and messages[-2].get("role", None) == "tool":
                # write the tool message first
                await cosmos_conversation_client.create_message(
                    uuid=str(uuid.uuid4()),
                    conversation_id=conversation_id,
                    user_id=user_id,
                    input_message=messages[-2],
                )
            # write the assistant message
            await cosmos_conversation_client.create_message(
                uuid=messages[-1]["id"],
                conversation_id=conversation_id,
                user_id=user_id,
                input_message=messages[-1],
            )
        else:
            raise Exception("No bot messages found")

        # Submit request to Chat Completions for response
        await cosmos_conversation_client.cosmosdb_client.close()
        response = {"success": True}
        return jsonify(response), 200

    except Exception as e:
        logging.exception("Exception in /history/update")
        return jsonify({"error": str(e)}), 500


@bp.route("/history/message_feedback", methods=["POST"])
async def update_message():
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]
    cosmos_conversation_client = init_cosmosdb_client()

    ## check request for message_id
    request_json = await request.get_json()
    message_id = request_json.get("message_id", None)
    message_feedback = request_json.get("message_feedback", None)
    try:
        if not message_id:
            return jsonify({"error": "message_id is required"}), 400

        if not message_feedback:
            return jsonify({"error": "message_feedback is required"}), 400

        ## update the message in cosmos
        updated_message = await cosmos_conversation_client.update_message_feedback(
            user_id, message_id, message_feedback
        )
        if updated_message:
            return (
                jsonify(
                    {
                        "message": f"Successfully updated message with feedback {message_feedback}",
                        "message_id": message_id,
                    }
                ),
                200,
            )
        else:
            return (
                jsonify(
                    {
                        "error": f"Unable to update message {message_id}. It either does not exist or the user does not have access to it."
                    }
                ),
                404,
            )

    except Exception as e:
        logging.exception("Exception in /history/message_feedback")
        return jsonify({"error": str(e)}), 500


@bp.route("/history/delete", methods=["DELETE"])
async def delete_conversation():
    ## get the user id from the request headers
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    ## check request for conversation_id
    request_json = await request.get_json()
    conversation_id = request_json.get("conversation_id", None)

    try:
        if not conversation_id:
            return jsonify({"error": "conversation_id is required"}), 400

        ## make sure cosmos is configured
        cosmos_conversation_client = init_cosmosdb_client()
        if not cosmos_conversation_client:
            raise Exception("CosmosDB is not configured or not working")

        ## delete the conversation messages from cosmos first
        deleted_messages = await cosmos_conversation_client.delete_messages(
            conversation_id, user_id
        )

        ## Now delete the conversation
        deleted_conversation = await cosmos_conversation_client.delete_conversation(
            user_id, conversation_id
        )

        await cosmos_conversation_client.cosmosdb_client.close()

        return (
            jsonify(
                {
                    "message": "Successfully deleted conversation and messages",
                    "conversation_id": conversation_id,
                }
            ),
            200,
        )
    except Exception as e:
        logging.exception("Exception in /history/delete")
        return jsonify({"error": str(e)}), 500


@bp.route("/history/list", methods=["GET"])
async def list_conversations():
    offset = request.args.get("offset", 0)
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    ## make sure cosmos is configured
    cosmos_conversation_client = init_cosmosdb_client()
    if not cosmos_conversation_client:
        raise Exception("CosmosDB is not configured or not working")

    ## get the conversations from cosmos
    conversations = await cosmos_conversation_client.get_conversations(
        user_id, offset=offset, limit=25
    )
    await cosmos_conversation_client.cosmosdb_client.close()
    if not isinstance(conversations, list):
        return jsonify({"error": f"No conversations for {user_id} were found"}), 404

    ## return the conversation ids

    return jsonify(conversations), 200


@bp.route("/history/read", methods=["POST"])
async def get_conversation():
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    ## check request for conversation_id
    request_json = await request.get_json()
    conversation_id = request_json.get("conversation_id", None)

    if not conversation_id:
        return jsonify({"error": "conversation_id is required"}), 400

    ## make sure cosmos is configured
    cosmos_conversation_client = init_cosmosdb_client()
    if not cosmos_conversation_client:
        raise Exception("CosmosDB is not configured or not working")

    ## get the conversation object and the related messages from cosmos
    conversation = await cosmos_conversation_client.get_conversation(
        user_id, conversation_id
    )
    ## return the conversation id and the messages in the bot frontend format
    if not conversation:
        return (
            jsonify(
                {
                    "error": f"Conversation {conversation_id} was not found. It either does not exist or the logged in user does not have access to it."
                }
            ),
            404,
        )

    # get the messages for the conversation from cosmos
    conversation_messages = await cosmos_conversation_client.get_messages(
        user_id, conversation_id
    )

    ## format the messages in the bot frontend format
    messages = [
        {
            "id": msg["id"],
            "role": msg["role"],
            "content": msg["content"],
            "createdAt": msg["createdAt"],
            "feedback": msg.get("feedback"),
        }
        for msg in conversation_messages
    ]

    await cosmos_conversation_client.cosmosdb_client.close()
    return jsonify({"conversation_id": conversation_id, "messages": messages}), 200


@bp.route("/history/rename", methods=["POST"])
async def rename_conversation():
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    ## check request for conversation_id
    request_json = await request.get_json()
    conversation_id = request_json.get("conversation_id", None)

    if not conversation_id:
        return jsonify({"error": "conversation_id is required"}), 400

    ## make sure cosmos is configured
    cosmos_conversation_client = init_cosmosdb_client()
    if not cosmos_conversation_client:
        raise Exception("CosmosDB is not configured or not working")

    ## get the conversation from cosmos
    conversation = await cosmos_conversation_client.get_conversation(
        user_id, conversation_id
    )
    if not conversation:
        return (
            jsonify(
                {
                    "error": f"Conversation {conversation_id} was not found. It either does not exist or the logged in user does not have access to it."
                }
            ),
            404,
        )

    ## update the title
    title = request_json.get("title", None)
    if not title:
        return jsonify({"error": "title is required"}), 400
    conversation["title"] = title
    updated_conversation = await cosmos_conversation_client.upsert_conversation(
        conversation
    )

    await cosmos_conversation_client.cosmosdb_client.close()
    return jsonify(updated_conversation), 200


@bp.route("/history/delete_all", methods=["DELETE"])
async def delete_all_conversations():
    ## get the user id from the request headers
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    # get conversations for user
    try:
        ## make sure cosmos is configured
        cosmos_conversation_client = init_cosmosdb_client()
        if not cosmos_conversation_client:
            raise Exception("CosmosDB is not configured or not working")

        conversations = await cosmos_conversation_client.get_conversations(
            user_id, offset=0, limit=None
        )
        if not conversations:
            return jsonify({"error": f"No conversations for {user_id} were found"}), 404

        # delete each conversation
        for conversation in conversations:
            ## delete the conversation messages from cosmos first
            deleted_messages = await cosmos_conversation_client.delete_messages(
                conversation["id"], user_id
            )

            ## Now delete the conversation
            deleted_conversation = await cosmos_conversation_client.delete_conversation(
                user_id, conversation["id"]
            )
        await cosmos_conversation_client.cosmosdb_client.close()
        return (
            jsonify(
                {
                    "message": f"Successfully deleted conversation and messages for user {user_id}"
                }
            ),
            200,
        )

    except Exception as e:
        logging.exception("Exception in /history/delete_all")
        return jsonify({"error": str(e)}), 500


@bp.route("/history/clear", methods=["POST"])
async def clear_messages():
    ## get the user id from the request headers
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    ## check request for conversation_id
    request_json = await request.get_json()
    conversation_id = request_json.get("conversation_id", None)

    try:
        if not conversation_id:
            return jsonify({"error": "conversation_id is required"}), 400

        ## make sure cosmos is configured
        cosmos_conversation_client = init_cosmosdb_client()
        if not cosmos_conversation_client:
            raise Exception("CosmosDB is not configured or not working")

        ## delete the conversation messages from cosmos
        deleted_messages = await cosmos_conversation_client.delete_messages(
            conversation_id, user_id
        )

        return (
            jsonify(
                {
                    "message": "Successfully deleted messages in conversation",
                    "conversation_id": conversation_id,
                }
            ),
            200,
        )
    except Exception as e:
        logging.exception("Exception in /history/clear_messages")
        return jsonify({"error": str(e)}), 500


@bp.route("/history/ensure", methods=["GET"])
async def ensure_cosmos():
    if not app_settings.chat_history:
        return jsonify({"error": "CosmosDB is not configured"}), 404

    try:
        cosmos_conversation_client = init_cosmosdb_client()
        success, err = await cosmos_conversation_client.ensure()
        if not cosmos_conversation_client or not success:
            if err:
                return jsonify({"error": err}), 422
            return jsonify({"error": "CosmosDB is not configured or not working"}), 500

        await cosmos_conversation_client.cosmosdb_client.close()
        return jsonify({"message": "CosmosDB is configured and working"}), 200
    except Exception as e:
        logging.exception("Exception in /history/ensure")
        cosmos_exception = str(e)
        if "Invalid credentials" in cosmos_exception:
            return jsonify({"error": cosmos_exception}), 401
        elif "Invalid CosmosDB database name" in cosmos_exception:
            return (
                jsonify(
                    {
                        "error": f"{cosmos_exception} {app_settings.chat_history.database} for account {app_settings.chat_history.account}"
                    }
                ),
                422,
            )
        elif "Invalid CosmosDB container name" in cosmos_exception:
            return (
                jsonify(
                    {
                        "error": f"{cosmos_exception}: {app_settings.chat_history.conversations_container}"
                    }
                ),
                422,
            )
        else:
            return jsonify({"error": "CosmosDB is not working"}), 500


async def generate_title(conversation_messages) -> str:
    ## make sure the messages are sorted by _ts descending
    title_prompt = "Summarize the conversation so far into a 4-word or less title. Do not use any quotation marks or punctuation. Do not include any other commentary or description."

    messages = [
        {"role": msg["role"], "content": msg["content"]}
        for msg in conversation_messages
    ]
    messages.append({"role": "user", "content": title_prompt})

    try:
        azure_openai_client = init_openai_client()
        response = await azure_openai_client.chat.completions.create(
            model=app_settings.azure_openai.model, messages=messages, temperature=1, max_tokens=64
        )

        title = response.choices[0].message.content
        return title
    except Exception as e:
        logging.exception("Exception while generating title", e)
        return messages[-2]["content"]


app = create_app()

if __name__ == '__main__':
    app.run(debug=False)