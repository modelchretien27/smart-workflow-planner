import os
import uuid
from datetime import datetime, date, timedelta
from typing import Optional, List

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

engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=300)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# IMPORTANT (à définir en variables d'environnement sur Render, jamais en dur en prod) :
SECRET_KEY = os.getenv("SECRET_KEY", "orbitflow-dev-secret-change-me-in-production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 jours

# Clé du FONDATEUR (toi) pour déverrouiller la console admin globale.
# Change impérativement cette valeur via la variable d'environnement FOUNDER_ADMIN_KEY sur Render.
FOUNDER_ADMIN_KEY = os.getenv("FOUNDER_ADMIN_KEY", "changeme-founder-key")

PLAN_PRICES = {"decouverte": 0.0, "business_pro": 29.0, "entreprise": 99.0}

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
    sector = Column(String, default="Autre")          # secteur / type d'entreprise
    plan = Column(String, default="decouverte")
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
    role = Column(String, default="employe")           # super_admin | superviseur | employe
    organization_id = Column(Integer, ForeignKey("organizations.id"))
    is_active = Column(Boolean, default=False)          # False tant que l'invitation n'est pas acceptée
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
    progress = Column(Float, default=0.0)
    deliverable = Column(String, nullable=True)
    department_id = Column(Integer, ForeignKey("departments.id"))
    organization_id = Column(Integer, ForeignKey("organizations.id"))

    due_date = Column(Date, nullable=True)
    new_due_date = Column(Date, nullable=True)
    assignee_name = Column(String, nullable=True)
    assignee_email = Column(String, nullable=True)
    validation_status = Column(String, default="en_cours")  # en_cours | en_attente | valide | rejete
    delay_reason = Column(Text, nullable=True)
    validated_by = Column(String, nullable=True)
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
    payment_method = Column(String, default="carte")
    status = Column(String, default="active")
    created_at = Column(DateTime, default=datetime.utcnow)


Base.metadata.create_all(bind=engine)

# NOTE DE MIGRATION IMPORTANTE :
# Si ta base existe déjà avec l'ancien schéma (departments/tasks sans les nouvelles colonnes),
# `create_all` ne modifiera PAS les tables existantes (il ne crée que celles qui manquent).
# Pour une base de production déjà peuplée, utilise Alembic pour migrer, ou repars d'une base
# vierge en développement. Sur Neon, le plus simple en phase de test est de supprimer les
# anciennes tables "departments" et "tasks" puis de relancer l'app pour qu'elle les recrée
# avec le nouveau schéma.

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
    allow_origins=["*"],  # En production, restreins à ton domaine front-end (Vercel) précis.
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


# ------------------------- Dépendances d'authentification -------------------------
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


class TaskCreate(BaseModel):
    title: str
    progress: float = 0.0
    department_id: int
    deliverable: Optional[str] = None
    due_date: Optional[date] = None
    assignee_name: Optional[str] = None
    assignee_email: Optional[EmailStr] = None


class TaskUpdate(BaseModel):
    progress: Optional[float] = None
    deliverable: Optional[str] = None
    due_date: Optional[date] = None
    assignee_name: Optional[str] = None
    assignee_email: Optional[EmailStr] = None
    delay_reason: Optional[str] = None
    new_due_date: Optional[date] = None


class TaskValidate(BaseModel):
    decision: str  # 'valide' | 'rejete'
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
    role: str = "employe"  # employe | superviseur


class InviteRequest(BaseModel):
    invites: List[InviteEntry]


class AcceptInvite(BaseModel):
    name: str
    password: str


class FeedbackCreate(BaseModel):
    message: str
    rating: int = 5


class SubscribeRequest(BaseModel):
    plan: str  # decouverte | business_pro | entreprise
    payment_method: str  # carte | mobile_money | virement | paypal


class FounderLogin(BaseModel):
    key: str


# helpers de sérialisation ----------------------------------------------
def serialize_user(u: UserDB) -> dict:
    return {
        "id": u.id, "name": u.name, "email": u.email, "role": u.role,
        "organization_id": u.organization_id, "is_active": u.is_active,
    }


def serialize_org(o: OrganizationDB) -> dict:
    return {
        "id": o.id, "name": o.name, "sector": o.sector, "plan": o.plan,
        "owner_email": o.owner_email, "created_at": o.created_at.isoformat() if o.created_at else None,
    }


def serialize_task(t: TaskDB) -> dict:
    return {
        "id": t.id, "title": t.title, "progress": t.progress, "deliverable": t.deliverable,
        "department_id": t.department_id, "organization_id": t.organization_id,
        "due_date": t.due_date.isoformat() if t.due_date else None,
        "new_due_date": t.new_due_date.isoformat() if t.new_due_date else None,
        "assignee_name": t.assignee_name, "assignee_email": t.assignee_email,
        "validation_status": t.validation_status, "delay_reason": t.delay_reason,
        "validated_by": t.validated_by,
    }


# =========================================================================
# 5. ENDPOINTS PUBLICS DE BASE
# =========================================================================
@app.get("/")
def read_root():
    return {"status": "online", "app": "OrbitFlow API", "docs": "/docs"}


@app.get("/health")
def health_check():
    return {"status": "healthy"}


# =========================================================================
# 6. AUTHENTIFICATION & GESTION DES UTILISATEURS
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
    user = db.query(UserDB).filter(UserDB.invite_token == token, UserDB.is_active == False).first()  # noqa: E712
    if not user:
        raise HTTPException(status_code=404, detail="Invitation introuvable ou déjà utilisée.")
    org = db.query(OrganizationDB).filter(OrganizationDB.id == user.organization_id).first()
    return {"email": user.email, "name": user.name, "role": user.role, "organization_name": org.name if org else ""}


@app.post("/auth/invite/{token}/accept")
def accept_invite(token: str, payload: AcceptInvite, db: Session = Depends(get_db)):
    user = db.query(UserDB).filter(UserDB.invite_token == token, UserDB.is_active == False).first()  # noqa: E712
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
# 7. DEPARTEMENTS & TACHES (multi-tenant, protégés)
# =========================================================================
@app.post("/departments/")
def create_department(dept: DepartmentCreate, current_user: UserDB = Depends(get_current_user),
                       db: Session = Depends(get_db)):
    db_dept = DepartmentDB(name=dept.name, organization_id=current_user.organization_id)
    db.add(db_dept)
    db.commit()
    db.refresh(db_dept)
    return db_dept


@app.get("/departments/")
def read_departments(current_user: UserDB = Depends(get_current_user), db: Session = Depends(get_db)):
    return db.query(DepartmentDB).filter(DepartmentDB.organization_id == current_user.organization_id).all()


@app.post("/tasks/")
def create_task(task: TaskCreate, current_user: UserDB = Depends(get_current_user), db: Session = Depends(get_db)):
    db_task = TaskDB(
        title=task.title, progress=task.progress, department_id=task.department_id,
        organization_id=current_user.organization_id, deliverable=task.deliverable,
        due_date=task.due_date, assignee_name=task.assignee_name, assignee_email=task.assignee_email,
    )
    db.add(db_task)
    db.commit()
    db.refresh(db_task)
    return serialize_task(db_task)


@app.get("/tasks/")
def read_tasks(current_user: UserDB = Depends(get_current_user), db: Session = Depends(get_db)):
    tasks = db.query(TaskDB).filter(TaskDB.organization_id == current_user.organization_id).all()
    return [serialize_task(t) for t in tasks]


@app.patch("/tasks/{task_id}")
def update_task(task_id: int, payload: TaskUpdate, current_user: UserDB = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    task = db.query(TaskDB).filter(TaskDB.id == task_id,
                                    TaskDB.organization_id == current_user.organization_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Tâche introuvable.")
    for field, value in payload.dict(exclude_unset=True).items():
        setattr(task, field, value)
    if payload.progress is not None and payload.progress >= 100:
        task.validation_status = "en_attente"
    db.commit()
    db.refresh(task)
    return serialize_task(task)


@app.patch("/tasks/{task_id}/validate")
def validate_task(task_id: int, payload: TaskValidate,
                   current_user: UserDB = Depends(require_roles("super_admin", "superviseur")),
                   db: Session = Depends(get_db)):
    task = db.query(TaskDB).filter(TaskDB.id == task_id,
                                    TaskDB.organization_id == current_user.organization_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Tâche introuvable.")
    if payload.decision not in ("valide", "rejete"):
        raise HTTPException(status_code=400, detail="Décision invalide.")
    task.validation_status = payload.decision
    task.validated_by = current_user.name
    db.commit()
    db.refresh(task)
    return serialize_task(task)


# =========================================================================
# 8. FEEDBACK / AVIS UTILISATEURS
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
@app.post("/billing/subscribe")
def subscribe(payload: SubscribeRequest, current_user: UserDB = Depends(require_roles("super_admin")),
              db: Session = Depends(get_db)):
    if payload.plan not in PLAN_PRICES:
        raise HTTPException(status_code=400, detail="Plan inconnu.")
    org = db.query(OrganizationDB).filter(OrganizationDB.id == current_user.organization_id).first()
    org.plan = payload.plan
    db.add(SubscriptionDB(
        organization_id=org.id, plan=payload.plan, price=PLAN_PRICES[payload.plan],
        payment_method=payload.payment_method, status="active",
    ))
    db.commit()
    # NOTE : ceci enregistre l'abonnement en base mais n'encaisse aucun paiement réel.
    # Pour un encaissement réel, connecte ici Stripe (carte), un agrégateur Mobile Money
    # (MTN MoMo / Orange Money API), ou une confirmation manuelle de virement bancaire.
    return {"status": "ok", "organization": serialize_org(org)}


# =========================================================================
# 10. CONSOLE FONDATEUR (toi) — vision globale, toutes entreprises confondues
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
    total_users = db.query(UserDB).filter(UserDB.is_active == True).count()  # noqa: E712
    total_departments = db.query(DepartmentDB).count()
    total_tasks = db.query(TaskDB).count()

    orgs = db.query(OrganizationDB).all()
    estimated_mrr = sum(PLAN_PRICES.get(o.plan, 0.0) for o in orgs)

    by_sector = {}
    for o in orgs:
        by_sector[o.sector or "Autre"] = by_sector.get(o.sector or "Autre", 0) + 1

    by_plan = {}
    for o in orgs:
        by_plan[o.plan] = by_plan.get(o.plan, 0) + 1

    subs = db.query(SubscriptionDB).all()
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

    return {
        "system_status": "En ligne",
        "estimated_mrr_usd": estimated_mrr,
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
        users_count = db.query(UserDB).filter(UserDB.organization_id == o.id, UserDB.is_active == True).count()  # noqa: E712
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

