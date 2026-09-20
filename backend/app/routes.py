from fastapi import APIRouter, UploadFile, File, HTTPException
import shutil
import json
import os
import re

from app.similarity_utils import cosine_similarity
from app.chunk_utils import split_text_into_chunks
from app.pdf_utils import extract_text_from_pdf
from app.gemini import ask_question
from app.embedding_utils import get_embedding
from app.database import SessionLocal
from app.models import PDF, PDFChunk, ChatMessage
from app.schemas import ChatRequest
from app.hash_utils import calculate_file_hash


router = APIRouter()


# =========================================================
# UPLOAD DIRECTORY
# =========================================================

UPLOAD_DIR = os.path.join(os.getcwd(), "uploads")

# Create uploads folder if it does not exist
os.makedirs(UPLOAD_DIR, exist_ok=True)


# =========================================================
# UPLOAD PDF
# =========================================================

@router.post("/upload")
async def upload_pdf(file: UploadFile = File(...)):
    db = SessionLocal()

    try:
        # Make sure uploads folder exists
        os.makedirs(UPLOAD_DIR, exist_ok=True)

        # Get only the filename
        original_filename = os.path.basename(file.filename)

        # Clean filename
        filename = re.sub(
            r"[^a-zA-Z0-9._-]",
            "_",
            original_filename
        )

        # Make sure file is a PDF
        if not filename.lower().endswith(".pdf"):
            raise HTTPException(
                status_code=400,
                detail="Only PDF files are allowed."
            )

        # Full file path
        file_path = os.path.join(
            UPLOAD_DIR,
            filename
        )

        # -------------------------------------------------
        # Save uploaded PDF
        # -------------------------------------------------

        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(
                file.file,
                buffer
            )

        # -------------------------------------------------
        # Calculate SHA-256 hash
        # -------------------------------------------------

        file_hash = calculate_file_hash(
            file_path
        )

        # -------------------------------------------------
        # Check if PDF already exists
        # -------------------------------------------------

        existing_pdf = (
            db.query(PDF)
            .filter(PDF.file_hash == file_hash)
            .first()
        )

        if existing_pdf:

            # Remove duplicate uploaded file
            if os.path.exists(file_path):
                os.remove(file_path)

            return {
                "message": "PDF already exists.",
                "pdf_id": existing_pdf.id,
                "filename": existing_pdf.filename
            }

        # -------------------------------------------------
        # Extract text from PDF
        # -------------------------------------------------

        extracted_text = extract_text_from_pdf(
            file_path
        )

        # -------------------------------------------------
        # Split text into chunks
        # -------------------------------------------------

        chunks = split_text_into_chunks(
            extracted_text
        )

        # -------------------------------------------------
        # Create PDF database record
        # -------------------------------------------------

        pdf_record = PDF(
            filename=filename,
            file_hash=file_hash,
            extracted_text=extracted_text
        )

        db.add(pdf_record)
        db.commit()
        db.refresh(pdf_record)

        # -------------------------------------------------
        # Save chunks and embeddings
        # -------------------------------------------------

        for index, chunk in enumerate(chunks):

            embedding = get_embedding(
                chunk
            )

            chunk_record = PDFChunk(
                pdf_id=pdf_record.id,
                chunk_number=index,
                chunk_text=chunk,
                embedding=json.dumps(embedding)
            )

            db.add(chunk_record)

        db.commit()

        # -------------------------------------------------
        # Success response
        # -------------------------------------------------

        return {
            "message": "PDF uploaded successfully!",
            "pdf_id": pdf_record.id,
            "filename": pdf_record.filename
        }

    except HTTPException:
        raise

    except Exception as e:

        # Rollback database changes
        db.rollback()

        # Remove partially uploaded file
        try:
            if (
                "file_path" in locals()
                and os.path.exists(file_path)
            ):
                os.remove(file_path)

        except Exception:
            pass

        raise HTTPException(
            status_code=500,
            detail=str(e)
        )

    finally:
        db.close()


# =========================================================
# CHAT
# =========================================================

@router.post("/chat")
async def chat(request: ChatRequest):

    db = SessionLocal()

    try:

        # -------------------------------------------------
        # Find PDF
        # -------------------------------------------------

        pdf = (
            db.query(PDF)
            .filter(PDF.id == request.pdf_id)
            .first()
        )

        if not pdf:
            raise HTTPException(
                status_code=404,
                detail="PDF not found."
            )

        # -------------------------------------------------
        # Save user's message
        # -------------------------------------------------

        user_message = ChatMessage(
            pdf_id=pdf.id,
            role="user",
            message=request.question
        )

        db.add(user_message)
        db.commit()

        # -------------------------------------------------
        # Generate question embedding
        # -------------------------------------------------

        question_embedding = get_embedding(
            request.question
        )

        # -------------------------------------------------
        # Get PDF chunks
        # -------------------------------------------------

        chunks = (
            db.query(PDFChunk)
            .filter(
                PDFChunk.pdf_id == pdf.id
            )
            .all()
        )

        # -------------------------------------------------
        # Calculate similarity
        # -------------------------------------------------

        scores = []

        for chunk in chunks:

            chunk_embedding = json.loads(
                chunk.embedding
            )

            similarity = cosine_similarity(
                question_embedding,
                chunk_embedding
            )

            scores.append(
                (similarity, chunk)
            )

        # -------------------------------------------------
        # Sort by similarity
        # -------------------------------------------------

        scores.sort(
            key=lambda x: x[0],
            reverse=True
        )

        # -------------------------------------------------
        # Get top 3 relevant chunks
        # -------------------------------------------------

        top_chunks = scores[:3]

        # -------------------------------------------------
        # Build context
        # -------------------------------------------------

        context = ""

        for _, chunk in top_chunks:

            context += (
                chunk.chunk_text
                + "\n\n"
            )

        # -------------------------------------------------
        # Ask Gemini
        # -------------------------------------------------

        answer = ask_question(
            context,
            request.question
        )

        # -------------------------------------------------
        # Save assistant response
        # -------------------------------------------------

        assistant_message = ChatMessage(
            pdf_id=pdf.id,
            role="assistant",
            message=answer
        )

        db.add(assistant_message)
        db.commit()

        # -------------------------------------------------
        # Return response
        # -------------------------------------------------

        return {
            "pdf_id": pdf.id,
            "filename": pdf.filename,
            "question": request.question,
            "answer": answer
        }

    finally:
        db.close()


# =========================================================
# DELETE PDF
# =========================================================

@router.delete("/delete/{pdf_id}")
async def delete_pdf(pdf_id: int):

    db = SessionLocal()

    try:

        # -------------------------------------------------
        # Find PDF
        # -------------------------------------------------

        pdf = (
            db.query(PDF)
            .filter(PDF.id == pdf_id)
            .first()
        )

        if not pdf:
            raise HTTPException(
                status_code=404,
                detail="PDF not found."
            )

        # -------------------------------------------------
        # Delete PDF chunks
        # -------------------------------------------------

        db.query(PDFChunk).filter(
            PDFChunk.pdf_id == pdf.id
        ).delete()

        # -------------------------------------------------
        # Delete chat messages
        # -------------------------------------------------

        db.query(ChatMessage).filter(
            ChatMessage.pdf_id == pdf.id
        ).delete()

        # -------------------------------------------------
        # Delete PDF file
        # -------------------------------------------------

        file_path = os.path.join(
            UPLOAD_DIR,
            pdf.filename
        )

        if os.path.exists(file_path):
            os.remove(file_path)

        # -------------------------------------------------
        # Delete PDF database record
        # -------------------------------------------------

        db.delete(pdf)

        db.commit()

        return {
            "message": "PDF deleted successfully."
        }

    finally:
        db.close()


# =========================================================
# GET ALL PDFs
# =========================================================

@router.get("/pdfs")
async def get_pdfs():

    db = SessionLocal()

    try:

        pdfs = db.query(PDF).all()

        return [
            {
                "pdf_id": pdf.id,
                "filename": pdf.filename
            }
            for pdf in pdfs
        ]

    finally:
        db.close()


# =========================================================
# GET SINGLE PDF
# =========================================================

@router.get("/pdf/{pdf_id}")
async def get_pdf(pdf_id: int):

    db = SessionLocal()

    try:

        pdf = (
            db.query(PDF)
            .filter(PDF.id == pdf_id)
            .first()
        )

        if not pdf:
            raise HTTPException(
                status_code=404,
                detail="PDF not found."
            )

        return {
            "pdf_id": pdf.id,
            "filename": pdf.filename
        }

    finally:
        db.close()


# =========================================================
# GET CHAT HISTORY
# =========================================================

@router.get("/chat/{pdf_id}")
async def get_chat_history(pdf_id: int):

    db = SessionLocal()

    try:

        # -------------------------------------------------
        # Check PDF exists
        # -------------------------------------------------

        pdf = (
            db.query(PDF)
            .filter(PDF.id == pdf_id)
            .first()
        )

        if not pdf:
            raise HTTPException(
                status_code=404,
                detail="PDF not found."
            )

        # -------------------------------------------------
        # Get chat messages
        # -------------------------------------------------

        messages = (
            db.query(ChatMessage)
            .filter(
                ChatMessage.pdf_id == pdf_id
            )
            .order_by(
                ChatMessage.created_at
            )
            .all()
        )

        # -------------------------------------------------
        # Return chat history
        # -------------------------------------------------

        return [
            {
                "id": message.id,
                "role": message.role,
                "message": message.message,
                "created_at": message.created_at
            }
            for message in messages
        ]

    finally:
        db.close()