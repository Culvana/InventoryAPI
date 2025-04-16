# function_app.py
import azure.functions as func
import azure.durable_functions as df
from azure.cosmos import CosmosClient
import logging
import os
from typing import List, Dict, Any, Optional
import datetime
from openai import AsyncOpenAI
import json

# Import the required functions from names.py
from shared.names import (
    standardize_item_name,
    evaluate_and_correct_item_name,
    process_inventory_item,
    load_jsonl_data,
    extract_final_name
)

# Initialize Durable Functions App
myapp = df.DFApp(http_auth_level=func.AuthLevel.ANONYMOUS)

# Initialize Cosmos DB client
cosmos_client = CosmosClient(
    os.environ["COSMOS_ENDPOINT"],
    os.environ["COSMOS_KEY"]
)

# Get database and container references
database_name = os.environ.get("COSMOS_DATABASE", "InvoicesDB")
source_container_name = os.environ.get("COSMOS_CONTAINER", "Invoices")
dest_container_name = "Inventory"

source_db = cosmos_client.get_database_client(database_name)
source_container = source_db.get_container_client(source_container_name)
dest_db = cosmos_client.get_database_client(database_name)

# Ensure Inventory container exists
try:
    dest_container = dest_db.get_container_client(dest_container_name)
    dest_container.read()
except Exception:
    dest_container = dest_db.create_container(
        id=dest_container_name,
        partition_key_path="/userId",
        offer_throughput=400
    )

def extract_items_from_invoice(invoice: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    """Safely extract items from an invoice document."""
    try:
        if not isinstance(invoice, dict):
            logging.warning(f"Invalid invoice format: {type(invoice)}")
            return None

        all_items = []
        
        # Check for 'invoices' list first (root level structure)
        if 'invoices' in invoice:
            for sub_invoice in invoice['invoices']:
                items = sub_invoice.get('List of Items') or sub_invoice.get('Items', [])
                if items:
                    for item in items:
                        item['Supplier Name'] = sub_invoice.get('Supplier Name', '')
                        item['Invoice Number'] = sub_invoice.get('Invoice Number', '')
                    all_items.extend(items)
            logging.info(f"Extracted {len(all_items)} items from nested invoices")
            return all_items

        # Check for direct items list
        items = invoice.get('List of Items') or invoice.get('Items', [])
        if items:
            for item in items:
                item['Supplier Name'] = invoice.get('Supplier Name', '')
                item['Invoice Number'] = invoice.get('Invoice Number', '')
            logging.info(f"Extracted {len(items)} items from direct invoice")
            return items

        logging.warning("No items found in invoice")
        return []

    except Exception as e:
        logging.error(f"Error extracting items from invoice: {str(e)}")
        return None

@app.route(route="process_items/{user_id}", methods=["POST"])
@app.durable_client_input(client_name="client")
async def http_start(req: func.HttpRequest, client: df.DurableOrchestrationClient) -> func.HttpResponse:
    """HTTP trigger to start processing items for a specific user."""
    try:
        user_id = req.route_params.get('user_id')
        if not user_id:
            return func.HttpResponse(
                "Please provide a user_id in the URL",
                status_code=400
            )

        instance_id = f"process_items_{user_id}_{datetime.datetime.now().strftime('%Y%m%d')}"
        
        # Check for existing instance
        existing_instance = await client.get_status(instance_id)
        if existing_instance and existing_instance.runtime_status in ["Running", "Pending"]:
            logging.info(f"Instance {instance_id} is already running")
            return client.create_check_status_response(req, instance_id)

        logging.info(f"Starting new orchestration with instance ID: {instance_id}")
        await client.start_new(
            "process_items_orchestrator",
            instance_id,
            {"user_id": user_id}
        )
        return client.create_check_status_response(req, instance_id)
    except Exception as e:
        logging.error(f"Error in HTTP start: {str(e)}")
        return func.HttpResponse(str(e), status_code=500)

@app.orchestration_trigger(context_name="context")
def process_items_orchestrator(context: df.DurableOrchestrationContext):
    """Main orchestrator function for processing user's items."""
    try:
        input_data = context.get_input()
        user_id = input_data.get('user_id')
        instance_id = context.instance_id
        
        logging.info(f"Starting orchestration {instance_id} for user {user_id}")
        
        # Get invoices for the specific user
        invoices = yield context.call_activity("get_user_invoices_activity", user_id)
        
        if not invoices:
            logging.warning(f"No invoices found for user {user_id}")
            return {
                "status": "completed",
                "message": f"No invoices found for user {user_id}",
                "processed_count": 0
            }

        # Extract all items from invoices
        all_items = []
        for invoice in invoices:
            items = extract_items_from_invoice(invoice)
            if items:
                for item in items:
                    all_items.append({
                        'item': item,
                        'invoice_id': invoice.get('id'),
                        'supplier': item.get('Supplier Name'),
                        'invoice_number': item.get('Invoice Number'),
                        'userId': user_id
                    })

        if not all_items:
            logging.warning(f"No items found to process for user {user_id}")
            return {
                "status": "completed",
                "message": "No items found to process",
                "processed_count": 0
            }

        logging.info(f"Processing {len(all_items)} items for user {user_id}")
        
        # Process items in batches
        processed_items = []
        batch_size = 10
        
        for i in range(0, len(all_items), batch_size):
            batch = all_items[i:i + batch_size]
            batch_tasks = [context.call_activity("process_single_item", item_data) for item_data in batch]
            
            batch_results = yield context.task_all(batch_tasks)
            valid_results = [r for r in batch_results if r is not None]
            processed_items.extend(valid_results)
            
            logging.info(f"Processed batch {i//batch_size + 1}/{(len(all_items) + batch_size - 1)//batch_size}, got {len(valid_results)} valid results")
            
            # Store batch results
            if valid_results:
                storage_result = yield context.call_activity(
                    "store_items_activity",
                    {
                        'items': valid_results,
                        'user_id': user_id,
                        'batch': i//batch_size + 1
                    }
                )
                logging.info(f"Batch {i//batch_size + 1} storage result: {storage_result}")

        return {
            "status": "completed",
            "processed_count": len(processed_items),
            "total_items": len(all_items),
            "instance_id": instance_id,
            "user_id": user_id
        }

    except Exception as e:
        logging.error(f"Error in orchestrator: {str(e)}")
        raise

@app.activity_trigger(input_name="userid")
async def get_user_invoices_activity(userid: str) -> List[Dict[str, Any]]:
    """Activity to get all invoices for a specific user."""
    try:
        query = f"SELECT * FROM c WHERE c.userId = '{userid}'"
        logging.info(f"Executing query: {query}")
        
        items = list(source_container.query_items(
            query=query,
            enable_cross_partition_query=True
        ))
        logging.info(f"Retrieved {len(items)} invoices for user {userid}")
        return items
    except Exception as e:
        logging.error(f"Error getting invoices for user {userid}: {str(e)}")
        raise

@app.activity_trigger(input_name="itemdata")
async def process_single_item(itemdata: Dict[str, Any]) -> Dict[str, Any]:
    """Activity to process a single inventory item."""
    try:
        client = AsyncOpenAI(api_key=os.getenv('OPENAI_API_KEY'))
        training_data = load_jsonl_data("training.jsonl")
        
        item = itemdata['item']
        user_id = itemdata['userId']
        
        logging.info(f"Processing item for user {user_id}")
        
        # Map raw item data into expected format
        processed_item = {
            'supplier': itemdata['supplier'],
            'invoice_id': itemdata['invoice_id'],
            'invoice_number': itemdata['invoice_number'],
            'userId': user_id,
            'itemName': item.get('Item Name', ''),
            'itemNumber': item.get('Item Number', ''),
            'quantityInCase': float(item.get('Quantity In a Case', 0)),
            'measurementOfEachItem': float(item.get('Measurement Of Each Item', 0)),
            'measuredIn': item.get('Measured In', ''),
            'totalUnitsOrdered': float(item.get('Total Units Ordered', 0)),
            'casePrice': float(item.get('Case Price', 0)),
            'catchWeight': item.get('Catch Weight', 'N/A'),
            'pricedBy': item.get('Priced By', ''),
            'splitable': item.get('Splitable', 'NO'),
            'splitPrice': item.get('Split Price', 'N/A'),
            'costOfUnit': float(item.get('Cost of a Unit', 0)),
            'productCategory': item.get('Product Category', '')
        }

        # Standardize item name
        standardized_name = await standardize_item_name(
            client=client,
            item_description=processed_item['itemName'],
            training_data=training_data,
            category=processed_item['productCategory']
        )
        
        # Evaluate and correct name
        evaluation = await evaluate_and_correct_item_name(
            client=client,
            standardized_name=standardized_name,
            original_name=processed_item['itemName'],
            category=processed_item['productCategory'],
            training_data=training_data
        )
        
        final_name = await extract_final_name(evaluation)
        evaluation2 = await evaluate_and_correct_item_name(
            client=client,
            standardized_name=final_name,
            original_name=processed_item['itemName'],
            category=processed_item['productCategory'],
            training_data=training_data
        )
        final_name2= await extract_final_name(evaluation2)
        if final_name2:
            processed_item['final_corrected_name'] = final_name2
            
        return processed_item
        
    except Exception as e:
        logging.error(f"Error processing item: {str(e)}")
        return None

@app.activity_trigger(input_name="storedata")
async def store_items_activity(storedata: Dict[str, Any]) -> Dict[str, Any]:
    """Activity to store processed items in destination container."""
    try:
        items = storedata['items']
        user_id = storedata['user_id']
        batch_num = storedata.get('batch', 0)
        
        logging.info(f"Starting storage for batch {batch_num}: {len(items)} items for user {user_id}")
        
        try:
            # Get existing user document
            query = f"SELECT * FROM c WHERE c.id = '{user_id}'"
            existing_docs = list(dest_container.query_items(
                query=query,
                enable_cross_partition_query=True
            ))
            
            # Create or get user document
            user_doc = existing_docs[0] if existing_docs else {
                'id': user_id,
                'userId': user_id,
                'supplier_name': items[0].get('supplier', '') if items else '',
                'items': [],
                'timestamp': datetime.datetime.utcnow().isoformat()
            }

            stored_count = 0
            errors = []
            
            for idx, item in enumerate(items):
                try:
                    if not item:
                        logging.warning(f"Skipping empty item at index {idx}")
                        continue

                    # Format item for storage with proper field mapping
                    storage_item = {
                        'Supplier Name': item.get('supplier', ''),
                        'Inventory Item Name': item.get('final_corrected_name', item.get('itemName', '')),
                        'Brand': extract_brand(item.get('itemName', '')),
                        'Inventory Unit of Measure': item.get('measuredIn', ''),
                        'Item Name': item.get('itemName', ''),
                        'Item Number': item.get('itemNumber', ''),
                        'Quantity In a Case': float(item.get('quantityInCase', 0)),
                        'Measurement Of Each Item': float(item.get('measurementOfEachItem', 0)),
                        'Measured In': item.get('measuredIn', ''),
                        'Total Units': float(item.get('totalUnitsOrdered', 0)),
                        'Case Price': float(item.get('casePrice', 0)),
                        'Catch Weight': item.get('catchWeight', 'N/A'),
                        'Priced By': item.get('pricedBy', ''),
                        'Splitable': item.get('splitable', 'NO'),
                        'Split Price': item.get('splitPrice', 'N/A'),
                        'Cost of a Unit': float(item.get('costOfUnit', 0)),
                        'Category': item.get('productCategory', ''),
                        'timestamp': datetime.datetime.utcnow().isoformat(),
                        'batchNumber': batch_num
                    }
                    
                    # Validate storage item
                    if validate_storage_item(storage_item):
                        user_doc['items'].append(storage_item)
                        stored_count += 1
                        logging.info(f"Added item {idx} for batch {batch_num}")
                    else:
                        error_msg = f"Invalid item data for item {idx}"
                        logging.error(error_msg)
                        errors.append(error_msg)
                    
                except Exception as e:
                    error_msg = f"Error storing item {idx} for user {user_id}: {str(e)}"
                    logging.error(error_msg)
                    errors.append(error_msg)
                    continue
            
            # Update document metadata
            user_doc['timestamp'] = datetime.datetime.utcnow().isoformat()
            user_doc['itemCount'] = len(user_doc['items'])
            
            # Store with retry logic
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    response = dest_container.upsert_item(body=user_doc)
                    break
                except Exception as e:
                    if attempt == max_retries - 1:
                        raise
                    await asyncio.sleep(1)
            
            return {
                "status": "success" if stored_count == len(items) else "partial_failure" if stored_count > 0 else "failure",
                "batch": batch_num,
                "stored_count": stored_count,
                "total_items": len(items),
                "errors": errors,
                "user_id": user_id
            }
            
        except Exception as e:
            logging.error(f"Error in document storage: {str(e)}")
            raise
            
    except Exception as e:
        logging.error(f"Critical error in store items activity: {str(e)}")
        raise

def extract_brand(item_name: str) -> str:
    """Extract brand name from item name."""
    try:
        words = item_name.split()
        if not words:
            return "Generic"
            
        # Check for common brand patterns
        if '/' in item_name:
            # Handle cases like "Brand/Product"
            return item_name.split('/')[0].strip()
            
        if "'" in item_name or "'" in item_name:
            # Handle cases like "Brand's Product"
            possessive_split = item_name.replace("'", "'").split("'")
            if len(possessive_split) > 1:
                return possessive_split[0].strip()
        
        # Default to first word if capitalized
        if words[0][0].isupper():
            return words[0]
            
        return "Generic"
    except Exception:
        return "Generic"

def validate_storage_item(item: Dict[str, Any]) -> bool:
    """Validate required fields in storage item."""
    required_fields = [
        'Inventory Item Name',
        'Item Number',
        'Category',
        'Measured In'
    ]
    
    # Check required fields
    for field in required_fields:
        if not item.get(field):
            logging.warning(f"Missing required field: {field}")
            return False
            
    # Validate numeric fields
    numeric_fields = [
        'Quantity In a Case',
        'Measurement Of Each Item',
        'Total Units',
        'Case Price',
        'Cost of a Unit'
    ]
    
    for field in numeric_fields:
        try:
            if not isinstance(item.get(field), (int, float)) or item.get(field) < 0:
                logging.warning(f"Invalid numeric value for {field}: {item.get(field)}")
                return False
        except Exception:
            logging.warning(f"Error validating numeric field {field}")
            return False
            
    return True
