import os
from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, Integer, String, Float, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session

# 1. Configuration de la base de données PostgreSQL (Neon.tech)
DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    # URL fallback en local si DATABASE_URL n'est pas définie
    DATABASE_URL = "sqlite:///./test.db"

# Correction pour SQLAlchemy en cas d’URL commençant par postgres://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# 2. Modèles de la Base de Données
class DepartmentDB(Base):
    __tablename__ = "departments"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True)

class TaskDB(Base):
    __tablename__ = "tasks"
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, index=True)
    progress = Column(Float, default=0.0)
    department_id = Column(Integer, ForeignKey("departments.id"))

Base.metadata.create_all(bind=engine)

# 3. Application FastAPI
app = FastAPI(
    title="Smart Workflow Planner API",
    description="API Back-end B2B pour la gestion des flux de travail et départements",
    version="1.0.0"
)

# Activation de CORS pour permettre les requêtes du Front-end Vercel
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Injection de dépendance pour la session DB
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# Schemas Pydantic
class TaskCreate(BaseModel):
    title: str
    progress: float
    department_id: int

class DepartmentCreate(BaseModel):
    name: str

# 4. Endpoints de l'API
@app.get("/")
def read_root():
    return {
        "status": "online",
        "app": "Smart Workflow Planner API",
        "docs": "/docs"
    }

@app.get("/health")
def health_check():
    return {"status": "healthy"}

@app.post("/departments/")
def create_department(dept: DepartmentCreate, db: Session = Depends(get_db)):
    db_dept = DepartmentDB(name=dept.name)
    db.add(db_dept)
    db.commit()
    db.refresh(db_dept)
    return db_dept

@app.get("/departments/")
def read_departments(db: Session = Depends(get_db)):
    return db.query(DepartmentDB).all()

@app.post("/tasks/")
def create_task(task: TaskCreate, db: Session = Depends(get_db)):
    db_task = TaskDB(title=task.title, progress=task.progress, department_id=task.department_id)
    db.add(db_task)
    db.commit()
    db.refresh(db_task)
    return db_task

@app.get("/tasks/")
def read_tasks(db: Session = Depends(get_db)):
    return db.query(TaskDB).all()
