import os
import uuid
import json
from datetime import datetime, date, timedelta
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from sqlalchemy import (
    create_engine, Column, Integer, String, Float, ForeignKey, Boolean,
    DateTime, Date, Text, func
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session, relationship
from passlib.context import CryptContext
from jose import jwt, JWTError

# =========================================================================
# 1. CONFIGURATION
# =========================================================================
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    DATABASE_URL = "sqlite:///./test.db"
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

SECRET_KEY = os.getenv("SECRET_KEY", "orbitflow-dev-secret-change-me-in-production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 jours

FOUNDER_ADMIN_KEY = os.getenv("FOUNDER_ADMIN_KEY", "changeme-founder-key")

PLAN_PRICES = {"decouverte": 0.0, "business_pro": 29.0, "entreprise": 99.0}
ANNUAL_DISCOUNT_MONTHS = {"decouverte": 0, "business_pro": 3, "entreprise": 4}


def annual_price(plan: str) -> float:
    monthly = PLAN_PRICES.get(plan, 0.0)
    months_billed = max(0, 12 - ANNUAL_DISCOUNT_MONTHS.get(plan, 0))
    return round(monthly * months_billed, 2)


def monthly_equivalent(price: float, billing_cycle: str) -> float:
    return round(price / 12, 2) if billing_cycle == "annual" else price

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    return pwd_context.verify(password, hashed)


def create_access_token(data: dict, expires_delta: timedelta):
    payload = data.copy()
    payload["exp"] = datetime.utcnow() + expires_delta
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=401, detail="Session invalide ou expirée. Merci de te reconnecter.")


# =========================================================================
# 2. MODELES DE BASE DE DONNEES
# =========================================================================
class OrganizationDB(Base):
    __tablename__ = "organizations"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, index=True)
    sector = Column(String, default="Autre")
    plan = Column(String, default="decouverte")
    logo_url = Column(String, nullable=True)
    owner_email = Column(String, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    users = relationship("UserDB", back_populates="organization")
    departments = relationship("DepartmentDB", back_populates="organization")


class UserDB(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String)
    email = Column(String, unique=True, index=True)
    password_hash = Column(String, nullable=True)
    role = Column(String, default="employe")
    organization_id = Column(Integer, ForeignKey("organizations.id"))
    is_active = Column(Boolean, default=False)
    invite_token = Column(String, unique=True, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    organization = relationship("OrganizationDB", back_populates="users")


class DepartmentDB(Base):
    __tablename__ = "departments"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"))

    organization = relationship("OrganizationDB", back_populates="departments")


class TaskDB(Base):
    __tablename__ = "tasks"
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, index=True)
    deliverables_json = Column(Text, default="[]")
    department_id = Column(Integer, ForeignKey("departments.id"))
    organization_id = Column(Integer, ForeignKey("organizations.id"))

    due_date = Column(Date, nullable=True)
    new_due_date = Column(Date, nullable=True)
    assignee_name = Column(String, nullable=True)
    assignee_email = Column(String, nullable=True)

    validation_status = Column(String, default="en_cours")
    supervisor_comment = Column(Text, nullable=True)
    validated_by = Column(String, nullable=True)

    created_by_name = Column(String, nullable=True)
    created_by_email = Column(String, nullable=True)
    created_by_role = Column(String, nullable=True)

    delay_reason = Column(Text, nullable=True)
    needs_attention = Column(Boolean, default=False)
    last_comment = Column(Text, nullable=True)
    last_comment_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)


class FeedbackDB(Base):
    __tablename__ = "feedback"
    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"))
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    author_name = Column(String, nullable=True)
    message = Column(Text)
    rating = Column(Integer, default=5)
    created_at = Column(DateTime, default=datetime.utcnow)


class SubscriptionDB(Base):
    __tablename__ = "subscriptions"
    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"))
    plan = Column(String)
    price = Column(Float, default=0.0)
    billing_cycle = Column(String, default="monthly")
    payment_method = Column(String, default="carte")
    status = Column(String, default="active")
    created_at = Column(DateTime, default=datetime.utcnow)


class WithdrawalDB(Base):
    __tablename__ = "withdrawals"
    id = Column(Integer, primary_key=True, index=True)
    amount = Column(Float, default=0.0)
    method = Column(String, default="virement")
    destination = Column(String, nullable=True)
    note = Column(Text, nullable=True)
    status = Column(String, default="en_attente")
    requested_at = Column(DateTime, default=datetime.utcnow)
    processed_at = Column(DateTime, nullable=True)


Base.metadata.create_all(bind=engine)

# =========================================================================
# 3. APPLICATION FASTAPI
# =========================================================================
app = FastAPI(
    title="OrbitFlow API",
    description="API Back-end B2B multi-entreprises pour la planification, la validation des livrables et la gestion des accès.",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_current_user(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)) -> UserDB:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authentification requise.")
    token = authorization.split(" ", 1)[1]
    payload = decode_token(token)
    user_id = payload.get("sub")
    user = db.query(UserDB).filter(UserDB.id == int(user_id)).first() if user_id else None
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="Compte introuvable ou désactivé.")
    return user


def require_roles(*roles):
    def checker(current_user: UserDB = Depends(get_current_user)):
        if current_user.role not in roles:
            raise HTTPException(status_code=403, detail="Action réservée à un rôle supérieur.")
        return current_user
    return checker


def get_current_founder(authorization: Optional[str] = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Accès fondateur requis.")
    token = authorization.split(" ", 1)[1]
    payload = decode_token(token)
    if not payload.get("founder"):
        raise HTTPException(status_code=403, detail="Ce jeton n'a pas les droits fondateur.")
    return True


# =========================================================================
# 4. SCHEMAS PYDANTIC
# =========================================================================
class DepartmentCreate(BaseModel):
    name: str


class DeliverableItem(BaseModel):
    id: Optional[str] = None
    text: str
    done: bool = False
    critical: bool = False


class TaskCreate(BaseModel):
    title: str
    department_id: int
    deliverables: List[DeliverableItem] = []
    due_date: Optional[date] = None
    assignee_name: Optional[str] = None
    assignee_email: Optional[EmailStr] = None


class TaskUpdate(BaseModel):
    title: Optional[str] = None
    department_id: Optional[int] = None
    deliverables: Optional[List[DeliverableItem]] = None
    due_date: Optional[date] = None
    assignee_name: Optional[str] = None
    assignee_email: Optional[EmailStr] = None
    delay_reason: Optional[str] = None
    new_due_date: Optional[date] = None
    needs_attention: Optional[bool] = None
    last_comment: Optional[str] = None


class DeliverableToggle(BaseModel):
    done: bool


class OrientationDecision(BaseModel):
    decision: str
    comment: Optional[str] = None


class RegisterOrganization(BaseModel):
    organization_name: str
    sector: str = "Autre"
    admin_name: str
    admin_email: EmailStr
    admin_password: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class InviteEntry(BaseModel):
    email: EmailStr
    name: Optional[str] = None
    role: str = "employe"


class InviteRequest(BaseModel):
    invites: List[InviteEntry]


class AcceptInvite(BaseModel):
    name: str
    password: str


class FeedbackCreate(BaseModel):
    message: str
    rating: int = 5


class SubscribeRequest(BaseModel):
    plan: str
    payment_method: str
    billing_cycle: str = "monthly"


class OrganizationUpdate(BaseModel):
    name: Optional[str] = None
    sector: Optional[str] = None
    logo_url: Optional[str] = None


class FounderLogin(BaseModel):
    key: str


class WithdrawalRequest(BaseModel):
    amount: float
    method: str
    destination: str
    note: Optional[str] = None


def serialize_user(u: UserDB) -> dict:
    return {
        "id": u.id, "name": u.name, "email": u.email, "role": u.role,
        "organization_id": u.organization_id, "is_active": u.is_active,
    }


def serialize_org(o: OrganizationDB) -> dict:
    return {
        "id": o.id, "name": o.name, "sector": o.sector, "plan": o.plan, "logo_url": o.logo_url,
        "owner_email": o.owner_email, "created_at": o.created_at.isoformat() if o.created_at else None,
    }


def get_deliverables(t: TaskDB) -> List[dict]:
    try:
        items = json.loads(t.deliverables_json or "[]")
        return items if isinstance(items, list) else []
    except (ValueError, TypeError):
        return []


def compute_progress(deliverables: List[dict]) -> float:
    if not deliverables:
        return 0.0
    done = sum(1 for d in deliverables if d.get("done"))
    return round(done / len(deliverables) * 100, 1)


def serialize_task(t: TaskDB) -> dict:
    deliverables = get_deliverables(t)
    return {
        "id": t.id, "title": t.title,
        "deliverables": deliverables,
        "progress": compute_progress(deliverables),
        "department_id": t.department_id, "organization_id": t.organization_id,
        "due_date": t.due_date.isoformat() if t.due_date else None,
        "new_due_date": t.new_due_date.isoformat() if t.new_due_date else None,
        "assignee_name": t.assignee_name, "assignee_email": t.assignee_email,
        "validation_status": t.validation_status, "delay_reason": t.delay_reason,
        "supervisor_comment": t.supervisor_comment, "validated_by": t.validated_by,
        "created_by_name": t.created_by_name, "created_by_email": t.created_by_email,
        "created_by_role": t.created_by_role,
        "needs_attention": bool(t.needs_attention),
        "last_comment": t.last_comment,
        "last_comment_at": t.last_comment_at.isoformat() if t.last_comment_at else None,
        "created_at": t.created_at.isoformat() if t.created_at else None,
    }


# =========================================================================
# 5. ENDPOINTS PUBLICS
# =========================================================================
@app.get("/")
def read_root():
    return {"status": "online", "app": "OrbitFlow API", "docs": "/docs"}


@app.get("/health")
def health_check():
    return {"status": "healthy"}


# =========================================================================
# 6. AUTHENTIFICATION & UTILISATEURS
# =========================================================================
@app.post("/auth/register")
def register_organization(payload: RegisterOrganization, db: Session = Depends(get_db)):
    if db.query(UserDB).filter(UserDB.email == payload.admin_email).first():
        raise HTTPException(status_code=400, detail="Cette adresse e-mail est déjà utilisée.")

    org = OrganizationDB(name=payload.organization_name, sector=payload.sector,
                          plan="decouverte", owner_email=payload.admin_email)
    db.add(org)
    db.commit()
    db.refresh(org)

    admin = UserDB(
        name=payload.admin_name, email=payload.admin_email,
        password_hash=hash_password(payload.admin_password),
        role="super_admin", organization_id=org.id, is_active=True,
    )
    db.add(admin)
    db.commit()
    db.refresh(admin)

    db.add(SubscriptionDB(organization_id=org.id, plan="decouverte", price=0.0,
                           payment_method="gratuit", status="active"))
    db.commit()

    token = create_access_token({"sub": str(admin.id)}, timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    return {"access_token": token, "user": serialize_user(admin), "organization": serialize_org(org)}


@app.post("/auth/login")
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(UserDB).filter(UserDB.email == payload.email).first()
    if not user or not user.password_hash or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="E-mail ou mot de passe incorrect.")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Compte pas encore activé. Vérifie ton invitation.")
    token = create_access_token({"sub": str(user.id)}, timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    org = db.query(OrganizationDB).filter(OrganizationDB.id == user.organization_id).first()
    return {"access_token": token, "user": serialize_user(user), "organization": serialize_org(org)}


@app.get("/auth/me")
def read_me(current_user: UserDB = Depends(get_current_user), db: Session = Depends(get_db)):
    org = db.query(OrganizationDB).filter(OrganizationDB.id == current_user.organization_id).first()
    return {"user": serialize_user(current_user), "organization": serialize_org(org)}


@app.post("/auth/invite")
def invite_members(payload: InviteRequest,
                    current_user: UserDB = Depends(require_roles("super_admin", "superviseur")),
                    db: Session = Depends(get_db)):
    results = []
    for entry in payload.invites:
        existing = db.query(UserDB).filter(UserDB.email == entry.email).first()
        if existing:
            results.append({"email": entry.email, "status": "déjà existant"})
            continue
        token = uuid.uuid4().hex
        new_user = UserDB(
            name=entry.name or entry.email.split("@")[0],
            email=entry.email, password_hash=None,
            role=entry.role if entry.role in ("employe", "superviseur") else "employe",
            organization_id=current_user.organization_id,
            is_active=False, invite_token=token,
        )
        db.add(new_user)
        db.commit()
        results.append({"email": entry.email, "status": "invité", "invite_token": token})
    return {"invites": results}


@app.get("/auth/invite/{token}")
def get_invite(token: str, db: Session = Depends(get_db)):
    user = db.query(UserDB).filter(UserDB.invite_token == token, UserDB.is_active == False).first()
    if not user:
        raise HTTPException(status_code=404, detail="Invitation introuvable ou déjà utilisée.")
    org = db.query(OrganizationDB).filter(OrganizationDB.id == user.organization_id).first()
    return {"email": user.email, "name": user.name, "role": user.role, "organization_name": org.name if org else ""}


@app.post("/auth/invite/{token}/accept")
def accept_invite(token: str, payload: AcceptInvite, db: Session = Depends(get_db)):
    user = db.query(UserDB).filter(UserDB.invite_token == token, UserDB.is_active == False).first()
    if not user:
        raise HTTPException(status_code=404, detail="Invitation introuvable ou déjà utilisée.")
    user.name = payload.name or user.name
    user.password_hash = hash_password(payload.password)
    user.is_active = True
    user.invite_token = None
    db.commit()
    db.refresh(user)
    token_jwt = create_access_token({"sub": str(user.id)}, timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    org = db.query(OrganizationDB).filter(OrganizationDB.id == user.organization_id).first()
    return {"access_token": token_jwt, "user": serialize_user(user), "organization": serialize_org(org)}


@app.get("/auth/team")
def list_team(current_user: UserDB = Depends(get_current_user), db: Session = Depends(get_db)):
    members = db.query(UserDB).filter(UserDB.organization_id == current_user.organization_id).all()
    return [
        {**serialize_user(m), "status": "actif" if m.is_active else "invitation en attente"}
        for m in members
    ]


# =========================================================================
# 7. DEPARTEMENTS & TACHES
# =========================================================================
@app.post("/departments/")
def create_department(dept: DepartmentCreate, current_user: UserDB = Depends(get_current_user),
                       db: Session = Depends(get_db)):
    clean_name = dept.name.strip()
    existing = (
        db.query(DepartmentDB)
        .filter(DepartmentDB.organization_id == current_user.organization_id,
                func.lower(DepartmentDB.name) == clean_name.lower())
        .first()
    )
    if existing:
        return existing
    db_dept = DepartmentDB(name=clean_name, organization_id=current_user.organization_id)
    db.add(db_dept)
    db.commit()
    db.refresh(db_dept)
    return db_dept


@app.get("/departments/")
def read_departments(current_user: UserDB = Depends(get_current_user), db: Session = Depends(get_db)):
    return db.query(DepartmentDB).filter(DepartmentDB.organization_id == current_user.organization_id).all()


@app.delete("/departments/{department_id}")
def delete_department(department_id: int, current_user: UserDB = Depends(require_roles("super_admin")),
                       db: Session = Depends(get_db)):
    dept = db.query(DepartmentDB).filter(DepartmentDB.id == department_id,
                                          DepartmentDB.organization_id == current_user.organization_id).first()
    if not dept:
        raise HTTPException(status_code=404, detail="Domaine introuvable.")
    in_use = db.query(TaskDB).filter(TaskDB.department_id == department_id).count()
    if in_use:
        raise HTTPException(status_code=400, detail=f"Ce domaine contient encore {in_use} tâche(s) ; réassigne-les avant de le supprimer.")
    db.delete(dept)
    db.commit()
    return {"status": "deleted", "id": department_id}


@app.patch("/organizations/me")
def update_organization(payload: OrganizationUpdate, current_user: UserDB = Depends(require_roles("super_admin")),
                         db: Session = Depends(get_db)):
    org = db.query(OrganizationDB).filter(OrganizationDB.id == current_user.organization_id).first()
    for field, value in payload.dict(exclude_unset=True).items():
        setattr(org, field, value)
    db.commit()
    db.refresh(org)
    return serialize_org(org)


def _apply_progress_side_effects(task: TaskDB, deliverables: List[dict]):
    progress = compute_progress(deliverables)
    if progress >= 100 and task.validation_status == "en_cours":
        task.validation_status = "archive"
        task.validated_by = "Archivage automatique (100% des livrables)"
    elif progress < 100 and task.validation_status == "archive":
        task.validation_status = "en_cours"
        task.validated_by = None


@app.post("/tasks/")
def create_task(task: TaskCreate, current_user: UserDB = Depends(get_current_user), db: Session = Depends(get_db)):
    deliverables = [{"id": uuid.uuid4().hex[:8], "text": d.text.strip(), "done": d.done, "critical": d.critical}
                     for d in task.deliverables if d.text and d.text.strip()]
    initial_status = "en_cours" if current_user.role in ("super_admin", "superviseur") else "en_attente"
    db_task = TaskDB(
        title=task.title, department_id=task.department_id,
        organization_id=current_user.organization_id,
        deliverables_json=json.dumps(deliverables),
        due_date=task.due_date, assignee_name=task.assignee_name, assignee_email=task.assignee_email,
        validation_status=initial_status,
        created_by_name=current_user.name, created_by_email=current_user.email, created_by_role=current_user.role,
    )
    db.add(db_task)
    db.commit()
    db.refresh(db_task)
    return serialize_task(db_task)


@app.get("/tasks/")
def read_tasks(current_user: UserDB = Depends(get_current_user), db: Session = Depends(get_db)):
    tasks = db.query(TaskDB).filter(TaskDB.organization_id == current_user.organization_id).all()
    return [serialize_task(t) for t in tasks]


def _get_org_task(task_id: int, current_user: UserDB, db: Session) -> TaskDB:
    task = db.query(TaskDB).filter(TaskDB.id == task_id,
                                    TaskDB.organization_id == current_user.organization_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Tâche introuvable.")
    return task


@app.patch("/tasks/{task_id}")
def update_task(task_id: int, payload: TaskUpdate, current_user: UserDB = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    task = _get_org_task(task_id, current_user, db)
    data = payload.dict(exclude_unset=True)
    deliverables_payload = data.pop("deliverables", None)
    last_comment_payload = data.pop("last_comment", None)

    reschedule_requested = "due_date" in data and data["due_date"] != task.due_date

    for field, value in data.items():
        setattr(task, field, value)

    if deliverables_payload is not None:
        clean = [{"id": d.get("id") or uuid.uuid4().hex[:8], "text": d.get("text", "").strip(),
                  "done": bool(d.get("done")), "critical": bool(d.get("critical"))}
                 for d in deliverables_payload if d.get("text", "").strip()]
        task.deliverables_json = json.dumps(clean)
        _apply_progress_side_effects(task, clean)

    if last_comment_payload:
        task.last_comment = last_comment_payload
        task.last_comment_at = datetime.utcnow()
        task.needs_attention = False

    if reschedule_requested:
        task.needs_attention = False

    if task.validation_status == "rejete" and (deliverables_payload is not None or "due_date" in data or "title" in data):
        task.validation_status = "en_attente"
        task.supervisor_comment = None

    db.commit()
    db.refresh(task)
    return serialize_task(task)


@app.patch("/tasks/{task_id}/deliverables/{deliverable_id}")
def toggle_deliverable(task_id: int, deliverable_id: str, payload: DeliverableToggle,
                        current_user: UserDB = Depends(get_current_user), db: Session = Depends(get_db)):
    task = _get_org_task(task_id, current_user, db)
    if task.validation_status in ("en_attente", "rejete"):
        raise HTTPException(status_code=403, detail="Cette tâche attend encore l'orientation du superviseur avant de pouvoir démarrer.")
    items = get_deliverables(task)
    found = False
    for d in items:
        if d.get("id") == deliverable_id:
            d["done"] = payload.done
            found = True
            break
    if not found:
        raise HTTPException(status_code=404, detail="Livrable introuvable.")
    task.deliverables_json = json.dumps(items)
    _apply_progress_side_effects(task, items)
    db.commit()
    db.refresh(task)
    return serialize_task(task)


@app.delete("/tasks/{task_id}")
def delete_task(task_id: int, current_user: UserDB = Depends(require_roles("super_admin", "superviseur")),
                 db: Session = Depends(get_db)):
    task = _get_org_task(task_id, current_user, db)
    db.delete(task)
    db.commit()
    return {"status": "deleted", "id": task_id}


@app.patch("/tasks/{task_id}/orientation")
def decide_orientation(task_id: int, payload: OrientationDecision,
                        current_user: UserDB = Depends(require_roles("super_admin", "superviseur")),
                        db: Session = Depends(get_db)):
    task = _get_org_task(task_id, current_user, db)
    if payload.decision not in ("valide", "rejete"):
        raise HTTPException(status_code=400, detail="Décision invalide.")
    if payload.decision == "rejete" and not (payload.comment and payload.comment.strip()):
        raise HTTPException(status_code=400, detail="Un commentaire est requis pour expliquer le rejet et donner une orientation.")
    task.validation_status = "en_cours" if payload.decision == "valide" else "rejete"
    task.supervisor_comment = payload.comment.strip() if payload.comment else None
    task.validated_by = current_user.name
    db.commit()
    db.refresh(task)
    return serialize_task(task)


# =========================================================================
# 8. FEEDBACK / AVIS
# =========================================================================
@app.post("/feedback/")
def create_feedback(payload: FeedbackCreate, current_user: UserDB = Depends(get_current_user),
                     db: Session = Depends(get_db)):
    fb = FeedbackDB(
        organization_id=current_user.organization_id, user_id=current_user.id,
        author_name=current_user.name, message=payload.message,
        rating=max(1, min(5, payload.rating)),
    )
    db.add(fb)
    db.commit()
    db.refresh(fb)
    return {"id": fb.id, "message": "Merci pour ton retour !"}


# =========================================================================
# 9. ABONNEMENTS / FACTURATION
# =========================================================================
@app.get("/billing/plans")
def get_plan_prices():
    return {
        plan: {
            "monthly": PLAN_PRICES[plan],
            "annual": annual_price(plan),
            "discount_months": ANNUAL_DISCOUNT_MONTHS[plan],
        }
        for plan in PLAN_PRICES
    }


@app.post("/billing/subscribe")
def subscribe(payload: SubscribeRequest, current_user: UserDB = Depends(require_roles("super_admin")),
              db: Session = Depends(get_db)):
    if payload.plan not in PLAN_PRICES:
        raise HTTPException(status_code=400, detail="Plan inconnu.")
    if payload.billing_cycle not in ("monthly", "annual"):
        raise HTTPException(status_code=400, detail="Cycle de facturation invalide.")
    org = db.query(OrganizationDB).filter(OrganizationDB.id == current_user.organization_id).first()
    org.plan = payload.plan
    price = annual_price(payload.plan) if payload.billing_cycle == "annual" else PLAN_PRICES[payload.plan]
    db.add(SubscriptionDB(
        organization_id=org.id, plan=payload.plan, price=price, billing_cycle=payload.billing_cycle,
        payment_method=payload.payment_method, status="active",
    ))
    db.commit()
    return {"status": "ok", "organization": serialize_org(org), "price_charged": price, "billing_cycle": payload.billing_cycle}


# =========================================================================
# 10. CONSOLE FONDATEUR
# =========================================================================
@app.post("/admin/login")
def admin_login(payload: FounderLogin):
    if payload.key != FOUNDER_ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Clé fondateur incorrecte.")
    token = create_access_token({"founder": True}, timedelta(hours=12))
    return {"access_token": token}


@app.get("/admin/stats")
def admin_stats(_: bool = Depends(get_current_founder), db: Session = Depends(get_db)):
    total_orgs = db.query(OrganizationDB).count()
    total_users = db.query(UserDB).filter(UserDB.is_active == True).count()
    total_departments = db.query(DepartmentDB).count()
    total_tasks = db.query(TaskDB).count()

    orgs = db.query(OrganizationDB).all()
    subs = db.query(SubscriptionDB).all()

    active_subs_by_org = {}
    for s in subs:
        if s.status == "active":
            active_subs_by_org[s.organization_id] = s
    estimated_mrr = sum(monthly_equivalent(s.price, s.billing_cycle) for s in active_subs_by_org.values())
    total_revenue_collected = sum(s.price or 0.0 for s in subs if s.plan != "decouverte")

    by_sector = {}
    for o in orgs:
        by_sector[o.sector or "Autre"] = by_sector.get(o.sector or "Autre", 0) + 1

    by_plan = {}
    for o in orgs:
        by_plan[o.plan] = by_plan.get(o.plan, 0) + 1

    monthly = {}
    for s in subs:
        key = s.created_at.strftime("%Y-%m") if s.created_at else "inconnu"
        if key not in monthly:
            monthly[key] = {"count": 0, "revenue": 0.0}
        monthly[key]["count"] += 1
        monthly[key]["revenue"] += s.price or 0.0
    subscriptions_by_month = [{"month": k, **v} for k, v in sorted(monthly.items())]

    yearly = {}
    for s in subs:
        key = s.created_at.strftime("%Y") if s.created_at else "inconnu"
        if key not in yearly:
            yearly[key] = {"count": 0, "revenue": 0.0}
        yearly[key]["count"] += 1
        yearly[key]["revenue"] += s.price or 0.0
    subscriptions_by_year = [{"year": k, **v} for k, v in sorted(yearly.items())]

    withdrawals = db.query(WithdrawalDB).all()
    total_withdrawn = sum(w.amount for w in withdrawals if w.status in ("en_attente", "complete"))
    available_balance = round(total_revenue_collected - total_withdrawn, 2)

    return {
        "system_status": "En ligne",
        "estimated_mrr_usd": estimated_mrr,
        "total_revenue_collected_usd": round(total_revenue_collected, 2),
        "available_balance_usd": available_balance,
        "total_organizations": total_orgs,
        "total_users": total_users,
        "total_departments": total_departments,
        "total_tasks": total_tasks,
        "organizations_by_sector": by_sector,
        "organizations_by_plan": by_plan,
        "subscriptions_by_month": subscriptions_by_month,
        "subscriptions_by_year": subscriptions_by_year,
    }


@app.get("/admin/organizations")
def admin_organizations(_: bool = Depends(get_current_founder), db: Session = Depends(get_db)):
    orgs = db.query(OrganizationDB).all()
    result = []
    for o in orgs:
        users_count = db.query(UserDB).filter(UserDB.organization_id == o.id, UserDB.is_active == True).count()
        tasks_count = db.query(TaskDB).filter(TaskDB.organization_id == o.id).count()
        result.append({**serialize_org(o), "users_count": users_count, "tasks_count": tasks_count})
    return result


@app.get("/admin/feedback")
def admin_feedback(_: bool = Depends(get_current_founder), db: Session = Depends(get_db)):
    rows = db.query(FeedbackDB).order_by(FeedbackDB.created_at.desc()).limit(100).all()
    out = []
    for r in rows:
        org = db.query(OrganizationDB).filter(OrganizationDB.id == r.organization_id).first()
        out.append({
            "id": r.id, "author_name": r.author_name, "organization_name": org.name if org else "—",
            "message": r.message, "rating": r.rating,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        })
    return out


def serialize_withdrawal(w: WithdrawalDB) -> dict:
    return {
        "id": w.id, "amount": w.amount, "method": w.method, "destination": w.destination,
        "note": w.note, "status": w.status,
        "requested_at": w.requested_at.isoformat() if w.requested_at else None,
        "processed_at": w.processed_at.isoformat() if w.processed_at else None,
    }


@app.get("/admin/withdrawals")
def list_withdrawals(_: bool = Depends(get_current_founder), db: Session = Depends(get_db)):
    rows = db.query(WithdrawalDB).order_by(WithdrawalDB.requested_at.desc()).all()
    return [serialize_withdrawal(w) for w in rows]


@app.post("/admin/withdrawals")
def request_withdrawal(payload: WithdrawalRequest, _: bool = Depends(get_current_founder), db: Session = Depends(get_db)):
    if payload.amount <= 0:
        raise HTTPException(status_code=400, detail="Le montant doit être positif.")
    if payload.method not in ("virement", "mobile_money", "paypal"):
        raise HTTPException(status_code=400, detail="Méthode de retrait invalide.")

    subs = db.query(SubscriptionDB).all()
    withdrawals = db.query(WithdrawalDB).all()
    total_revenue = sum(s.price or 0.0 for s in subs if s.plan != "decouverte")
    total_withdrawn = sum(w.amount for w in withdrawals if w.status in ("en_attente", "complete"))
    available = total_revenue - total_withdrawn
    if payload.amount > available:
        raise HTTPException(status_code=400, detail=f"Solde insuffisant : {available:.2f}$ disponibles.")

    w = WithdrawalDB(amount=payload.amount, method=payload.method, destination=payload.destination,
                      note=payload.note, status="en_attente")
    db.add(w)
    db.commit()
    db.refresh(w)
    return serialize_withdrawal(w)


@app.patch("/admin/withdrawals/{withdrawal_id}")
def update_withdrawal_status(withdrawal_id: int, status: str, _: bool = Depends(get_current_founder), db: Session = Depends(get_db)):
    if status not in ("en_attente", "complete", "rejete"):
        raise HTTPException(status_code=400, detail="Statut invalide.")
    w = db.query(WithdrawalDB).filter(WithdrawalDB.id == withdrawal_id).first()
    if not w:
        raise HTTPException(status_code=404, detail="Retrait introuvable.")
    w.status = status
    w.processed_at = datetime.utcnow() if status in ("complete", "rejete") else None
    db.commit()
    db.refresh(w)
    return serialize_withdrawal(w)
