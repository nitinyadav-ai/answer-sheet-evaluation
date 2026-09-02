import sys
import os
import json
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

def main():
    # Load environment variables from a .env file if it exists
    load_dotenv()

    if len(sys.argv) < 2:
        print(json.dumps({"error": "Usage: python3 fetch_answers.py <question_id_1> <question_id_2> ..."}))
        sys.exit(1)

    question_ids = sys.argv[1:]

    # Database connection parameters from environment variables (with defaults)
    db_host = os.environ.get("DB_HOST", "localhost")
    db_port = os.environ.get("DB_PORT", "5432")
    db_name = os.environ.get("DB_NAME", "postgres")
    db_user = os.environ.get("DB_USER", "postgres")
    db_password = os.environ.get("DB_PASSWORD", "")
    table_name = os.environ.get("DB_TABLE", "questions")

    # Determine the paper prefixes to fetch all questions for the exam
    # This prevents dropping questions that were missed by the QR scanner
    import re
    prefixes = list(set([re.match(r'^(.+?)_Q\d+', qid).group(1) for qid in question_ids if re.match(r'^(.+?)_Q\d+', qid)]))

    try:
        # Establish connection
        conn = psycopg2.connect(
            host=db_host,
            port=db_port,
            dbname=db_name,
            user=db_user,
            password=db_password
        )
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        if prefixes:
            prefix_conditions = " OR ".join([f"question_id LIKE %s" for _ in prefixes])
            prefix_params = [f"{prefix}\\_Q%" for prefix in prefixes]
            
            query = f"""
                SELECT question_id, question, answer, type, subject, marks 
                FROM {table_name} 
                WHERE question_id = ANY(%s) OR {prefix_conditions};
            """
            cursor.execute(query, [question_ids] + prefix_params)
        else:
            query = f"""
                SELECT question_id, question, answer, type, subject, marks 
                FROM {table_name} 
                WHERE question_id = ANY(%s);
            """
            cursor.execute(query, (question_ids,))
            
        rows = cursor.fetchall()

        # Format as a dictionary mapping question_id to its data
        result = {row['question_id']: dict(row) for row in rows}
        
        # Identify any missing IDs for visibility
        missing = [qid for qid in question_ids if qid not in result]
        if missing:
            result["_missing"] = missing

        # Print JSON directly to stdout for the agent to hold in context
        print(json.dumps(result, indent=2))

    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
    finally:
        if 'cursor' in locals() and cursor:
            cursor.close()
        if 'conn' in locals() and conn:
            conn.close()

if __name__ == "__main__":
    main()