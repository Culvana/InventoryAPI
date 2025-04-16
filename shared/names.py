import csv
import json
import os
import logging
import pandas as pd
from typing import List, Dict, Any, Optional

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Constants for term removal and category rules
terms_to_remove = [
    'plastic', 'plst', 'refrigerator', 'tff', 'shelf-stable', 'cnt', 'box', 'bx',
    'bag', 'bg', 'case', 'cs', 'each', 'ea', 'bags', '1 piece', 'homemade', 'shelf',
    'count', 'vacuum', 'refrigerated', 'premium', 'packet', 'pack', 'pkg', 'container',
    'bottle', 'jar', 'tin', 'tub', 'tube', 'can', 'pouch', 'single', 'dozen', 'pair',
    'bulk', 'multiple', 'variety pack', 'frozen', 'chilled', 'dry', 'wet',
    'cured', 'smoked', 'dried', 'standard', 'natural', 'organic', 'artisan', 'gourmet',
    'choice', 'select', 'grade A', 'top quality', 'chopped', 'crushed', 'diced', 'minced', 
    'ready to eat', 'local'
]

category_specific_rules = {
    "paper goods and Disposables": ["Include size and material", "Remove brand names unless essential"],
    "BAKERY": ["Specify bread type first", "Include shape and size if relevant"],
    "produce": ["Include 'Fresh' for non-processed items", "Specify variety and form"],
    "meat": ["Include cut and preparation method", "Specify key characteristics"],
    "seafood": ["Include type and preparation method", "Specify fresh or frozen if relevant"],
    "dairy": ["Specify product type first", "Include key descriptors"],
    "Dry Grocery": ["Specify item type first", "Include key characteristics"],
    "beverages": ["Specify type of beverage first", "Include key descriptors like brand name"]
}

def load_jsonl_data(filename: str) -> List[Dict[str, Any]]:
    """Load and process JSONL training data with category-specific examples."""
    processed_data = []
    try:
        with open(filename, 'r', encoding='utf-8') as file:
            for line in file:
                try:
                    category_data = json.loads(line.strip())
                    if isinstance(category_data, dict) and 'examples' in category_data:
                        for example in category_data['examples']:
                            if isinstance(example, dict):
                                example['category'] = category_data.get('category', '')
                                processed_data.append(example)
                    elif isinstance(category_data, dict):
                        processed_data.append(category_data)
                except json.JSONDecodeError as e:
                    logger.warning(f"Skipping invalid JSON line: {line.strip()}: {e}")
                    continue
    except FileNotFoundError:
        logger.error(f"File '{filename}' not found.")
    except Exception as e:
        logger.error(f"Error processing JSONL file '{filename}': {e}")

    return processed_data

def load_data_file(filename: str) -> List[Dict[str, Any]]:
    """Load data from either CSV or Excel file."""
    data = []
    try:
        if filename.endswith('.csv'):
            df = pd.read_csv(filename)
        elif filename.endswith('.xlsx') or '_20' in filename:
            df = pd.read_excel(filename)
        else:
            logger.error(f"Unsupported file format: {filename}")
            return data

        data = df.to_dict('records')
        if not data:
            logger.warning(f"No data found in file: {filename}")
        else:
            logger.info(f"Successfully loaded {len(data)} records from {filename}")
        return data

    except Exception as e:
        logger.error(f"Error loading file {filename}: {str(e)}")
        return data

async def standardize_item_name(client, item_description: str, training_data: List[Dict[str, Any]], 
                              category: str) -> str:
    """Standardize an inventory item name using category-specific rules and training data."""
    category_examples = [
        ex for ex in training_data
        if isinstance(ex, dict) and ex.get('category', '').lower() == category.lower()
    ]

    training_examples = "\n".join([
        f"Input: {example.get('prompt', '')}\nOutput: {example.get('completion', '')}"
        for example in category_examples 
    ])
    
    category_rules = category_specific_rules.get(category, [])
    category_rules_str = "\n".join(f"- {rule}" for rule in category_rules)

    prompt = f"""As a restaurant inventory manager, standardize this inventory item name using these guidelines:
{training_examples}
{category_rules_str}
Item Name: {item_description}

Rules for Food Items:
1. Format: Ingredient, Description
2. Ingredient should come first, followed by a comma and then the description
3. Use 2-4 words total
4. Capitalize only the first letter of the ingredient
5. Remove all packaging information from the name

Rules for Non-Food Items:
1. Include size and essential details in the name
2. Use 2-4 words total
3. Capitalize the first letter of each word

General Rules:
4. Remove brand names unless essential
5. Use well-known culinary abbreviations
6. Remove terms like "refrigerator," "plastic," "shelf-stable"
7. Remove place names
8. For produce, include "Fresh" only if distinguishing
9. For meat and seafood, include cut and preparation
10. For dairy, specify type
11. For spices, include form
12. Remove terms: {', '.join(terms_to_remove)}
13. Keep essential descriptors

Task: Standardize this item description: {item_description}"""

    try:
        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "You are an AI assistant that standardizes inventory item names for a restaurant."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.1,
            max_tokens=15000
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"Error standardizing item name: {str(e)}")
        return item_description

async def evaluate_and_correct_item_name(client, standardized_name: str, original_name: str, 
                                       category: str, training_data: List[Dict[str, Any]]) -> str:
    """Evaluate and correct a standardized item name."""
    try:
        category_examples = [
            ex for ex in training_data 
            if isinstance(ex, dict) and ex.get('category', '').lower() == category.lower()
        ][:5]  # Limit to 5 examples
        
        training_examples = "\n".join([
            f"Input: {example.get('prompt', '')}\nOutput: {example.get('completion', '')}"
            for example in category_examples
        ])
        
        category_rules = category_specific_rules.get(category, [])
        category_rules_str = "\n".join(f"- {rule}" for rule in category_rules)

        prompt = f"""Evaluate and correct the following standardized inventory item name:
Standardized name: {standardized_name}
Original name: {original_name}
Category rules:
{category_rules_str}

Training examples:
{training_examples}

Terms to remove: {', '.join(terms_to_remove)}

Instructions:
- Put core item first
- Focus on fundamental identity
- Use "Core Item, Descriptors" format
- Remove unnecessary words
- Include brand names for beverages if well-known
- Keep standard measurements when relevant

Final corrected name: [Your corrected name]
Explanation: [Brief explanation]"""

        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "You are an AI assistant that evaluates and corrects standardized inventory item names."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.1,
            max_tokens=15000
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"Error evaluating item name: {str(e)}")
        return f"Error: {str(e)}"

async def extract_final_name(evaluation_result: str) -> Optional[str]:
    """Extract the final corrected name from the evaluation result."""
    try:
        evaluation_lines = evaluation_result.split('\n')
        for line in evaluation_lines:
            if line.lower().startswith("final corrected name:"):
                return line.split(":", 1)[1].strip()
        return None
    except Exception as e:
        logger.error(f"Error extracting final name: {str(e)}")
        return None

async def process_inventory_item(client, item: Dict[str, Any], file_name: str, 
                               training_data: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Process a single inventory item through standardization and correction."""
    try:
        item_name = item.get('Item Name', '')
        if not item_name.strip():
            logger.warning(f"Empty item name in file {file_name}")
            return None

        category = str(item.get('Product Category', '')).lower()
        logger.info(f"Processing {item_name} in category {category}")

        # Initial standardization
        standardized_name = await standardize_item_name(client, item_name, training_data, category)
        logger.info(f"Standardized: {standardized_name}")

        # Final item data extraction
        prompt = f"""Extract the following information:
1. Supplier Name
2. Inventory Item Name: {standardized_name}
3. Brand
4. Inventory Unit of Measure
5. Item Name 
6. Item Number
7. Quantity In a Case
8. Measurement Of Each Item
9. Measured In
10. Total Units
11. Case Price
12. Catch Weight
13. Priced By
14. Splitable
15. Split Price
16. Cost of a Unit
17. Category

Item data: {json.dumps(item)}
Return ONLY a JSON object with the above fields."""

        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "You are an AI assistant that extracts inventory information."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.1,
            max_tokens=15000,
            response_format={"type": "json_object"}
        )
        
        processed_item = json.loads(response.choices[0].message.content)
        processed_item['standardized_name'] = standardized_name
        processed_item['original_name'] = item_name
        return processed_item

    except Exception as e:
        logger.error(f"Error processing item: {str(e)}")
        return None

async def process_inventory_file(client, file: str, 
                               training_data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Process all items in a single inventory file."""
    try:
        from tqdm import tqdm
        
        data = load_data_file(file)
        processed_items = []
        
        for item in tqdm(data, desc=f"Processing {file}"):
            processed_item = await process_inventory_item(client, item, file, training_data)
            if processed_item:
                processed_items.append(processed_item)
                
        return processed_items
    except Exception as e:
        logger.error(f"Error processing file {file}: {str(e)}")
        return []

async def process_inventory_files(client, folder_path: str, 
                                training_data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Process all inventory files in the folder."""
    try:
        all_items = []
        files = [f for f in os.listdir(folder_path) 
                if f.endswith(('.xlsx', '.csv')) or '_20' in f]
        
        if not files:
            logger.error(f"No valid files found in {folder_path}")
            return []
            
        for file in files:
            try:
                file_path = os.path.join(folder_path, file)
                items = await process_inventory_file(client, file_path, training_data)
                if items:
                    all_items.extend(items)
                    logger.info(f"Processed {len(items)} items from {file}")
            except Exception as e:
                logger.error(f"Error processing {file}: {str(e)}")
                continue
                
        return all_items
    except Exception as e:
        logger.error(f"Error processing files: {str(e)}")
        return []

def save_results_to_file(results: List[Dict[str, Any]], filename: str) -> None:
    """Save processed results to a CSV file."""
    try:
        df = pd.DataFrame(results)
        df['Last Updated At'] = pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S')
        df['Active'] = 'Yes'
        df.to_csv(filename, index=False, encoding='utf-8')
        logger.info(f"Saved {len(results)} items to {filename}")
    except Exception as e:
        logger.error(f"Error saving results: {str(e)}")
        raise