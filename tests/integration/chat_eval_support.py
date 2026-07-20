from __future__ import annotations

import os
import sys
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from alembic import command
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from atguigu_ai.api.dependencies import AuthRouteDependencies
from atguigu_ai.api.routes.chat import ChatRouteDependencies
from atguigu_ai.api.server import create_app
from atguigu_ai.auth import (
    Account,
    AccountRole,
    AccountUserBinding,
    AuthService,
    BusinessIdentityResolver,
    CredentialTokenPurpose,
    PasswordHasher,
    RedisCredentialTokenStore,
    RedisSessionStore,
)
from atguigu_ai.email import FakeEmailDelivery
from tests.evaluation.chat_eval_cases import ChatEvalCase
from tests.integration.test_account_migration import (
    _alembic_config,
    _isolated_mysql_database,
    _target_name,
)
from tests.integration.test_auth_service_mysql_redis import RepositoryUnitOfWork
from tests.integration.test_chat_authorization_http import NOW, PASSWORD, SessionScopedBindingRepository
from tests.integration.test_redis_session import client as redis_client


ROOT = Path(__file__).resolve().parents[2]
ECS_DEMO = ROOT / "ecs_demo"
if str(ECS_DEMO) not in sys.path:
    sys.path.insert(0, str(ECS_DEMO))

from actions.db_table_class import (  # noqa: E402
    Base,
    Logistics,
    OrderDetail,
    OrderInfo,
    OrderStatus,
    Postsale,
    PostsaleReason,
    PostsaleStatus,
    ProductCategory,
    ReceiveInfo,
    Region,
    SkuInfo,
    UserInfo,
)


EVAL_EMAIL = "llm-eval@example.com"
EVAL_USER_ID = "eval-user"
VERIFY_TOKEN = "E" * 43
AUTH_SEED_VERSION = "eval-seed-v1"
TZ = timezone.utc
OLD_PROVINCE = "浙江省"
OLD_CITY = "杭州市"
OLD_DISTRICT = "西湖区"
NEW_PROVINCE = "上海市"
NEW_CITY = "上海市"
NEW_DISTRICT = "浦东新区"
NEW_STREET = "测试路 88 号 1602"
NEW_FULL_ADDRESS = f"{NEW_PROVINCE}{NEW_DISTRICT}{NEW_STREET}"
OLD_STREET = "文三路 1 号"
POSTSALE_REASON_REFUND = "商品有质量问题"
POSTSALE_REASON_EXCHANGE = "尺码不合适"


@dataclass(frozen=True)
class EvalMetrics:
    total_cases: int
    scenario_completion_rate: float
    business_fact_accuracy: float
    boundary_refusal_rate: float
    average_turns_to_completion: float


@dataclass(frozen=True)
class EvalResult:
    case_id: str
    completed: bool
    factually_correct: bool
    boundary_correct: bool
    turns: int


@dataclass
class EvalContext:
    engine: Engine
    session_factory: sessionmaker[Session]
    client: httpx.AsyncClient
    redis: Any
    email: FakeEmailDelivery
    account_id: str
    business_user_id: str
    verification_token: str
    _database_context: Any
    _action_db_restore: tuple[Any, Any, Any, Any]

    async def aclose(self) -> None:
        await self.client.aclose()
        with suppress(Exception):
            await self.redis.flushdb()
        with suppress(Exception):
            await self.redis.aclose()
        action_db, original_engine, original_session_local, original_url = self._action_db_restore
        action_db.engine = original_engine
        action_db.SessionLocal = original_session_local
        action_db.url = original_url
        self.engine.dispose()
        self._database_context.__exit__(None, None, None)


async def make_eval_context() -> EvalContext:
    database_context = _isolated_mysql_database()
    database_url = database_context.__enter__()
    previous_target = os.environ.get("MIGRATION_EXPECTED_TARGET")
    os.environ["MIGRATION_EXPECTED_TARGET"] = _target_name(database_url)
    try:
        command.upgrade(_alembic_config(database_url), "head")
    finally:
        if previous_target is None:
            os.environ.pop("MIGRATION_EXPECTED_TARGET", None)
        else:
            os.environ["MIGRATION_EXPECTED_TARGET"] = previous_target

    engine = create_engine(database_url, pool_pre_ping=True)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    action_db_restore = _patch_action_database(session_factory, engine)
    _seed_eval_business_data(session_factory)

    redis = redis_client()
    await redis.flushdb()
    email = FakeEmailDelivery()
    sessions = RedisSessionStore(redis, clock=lambda: NOW)
    service = AuthService(
        uow_factory=lambda: RepositoryUnitOfWork(session_factory),
        password_hasher=PasswordHasher(),
        credential_tokens=RedisCredentialTokenStore(
            redis,
            ttl_seconds={
                CredentialTokenPurpose.verify_email: 300,
                CredentialTokenPurpose.reset_password: 300,
            },
            token_factory=lambda: VERIFY_TOKEN,
            clock=lambda: NOW,
        ),
        sessions=sessions,
        email_delivery=email,
        public_base_url="https://customer.example.test/auth",
        clock=lambda: NOW,
    )
    app = create_app(
        auth_deps=AuthRouteDependencies(
            service=service,
            sessions=sessions,
            cookie_secure=False,
        ),
        chat_deps=ChatRouteDependencies(
            agent=_load_production_agent(),
            business_identity_resolver=BusinessIdentityResolver(
                SessionScopedBindingRepository(session_factory)
            ),
        ),
        enable_inspect=False,
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
    )
    account_id, business_user_id = await _register_and_bind(client, engine, VERIFY_TOKEN)
    return EvalContext(
        engine=engine,
        session_factory=session_factory,
        client=client,
        redis=redis,
        email=email,
        account_id=account_id,
        business_user_id=business_user_id,
        verification_token=VERIFY_TOKEN,
        _database_context=database_context,
        _action_db_restore=action_db_restore,
    )


async def evaluate_case(context: EvalContext, case: ChatEvalCase) -> EvalResult:
    _reset_eval_business_data(context.session_factory)
    await context.client.post(
        "/api/chat/reset",
        headers={"X-CSRF-Token": context.client.cookies.get("auth_csrf")},
    )
    expected = _fixture_values(context.engine, context.business_user_id, case.fixture_key)
    response_text, turns = await _run_case(context, case, expected)
    completed = _score_completion(case, response_text, context.engine, expected)
    factual = _score_factual(case, response_text, context.engine, expected)
    boundary = _score_boundary(case, response_text) if case.expectation == "boundary" else True
    return EvalResult(
        case_id=case.case_id,
        completed=completed,
        factually_correct=factual,
        boundary_correct=boundary,
        turns=turns,
    )


def aggregate_metrics(results: list[EvalResult]) -> EvalMetrics:
    task_results = [item for item in results if not item.case_id.startswith("boundary_")]
    boundary_results = [item for item in results if item.case_id.startswith("boundary_")]
    completed = [item for item in task_results if item.completed]
    return EvalMetrics(
        total_cases=len(results),
        scenario_completion_rate=(
            sum(1 for item in task_results if item.completed) / len(task_results)
            if task_results
            else 0.0
        ),
        business_fact_accuracy=(
            sum(1 for item in task_results if item.factually_correct) / len(task_results)
            if task_results
            else 0.0
        ),
        boundary_refusal_rate=(
            sum(1 for item in boundary_results if item.boundary_correct) / len(boundary_results)
            if boundary_results
            else 0.0
        ),
        average_turns_to_completion=(
            sum(item.turns for item in completed) / len(completed)
            if completed
            else 0.0
        ),
    )


def _load_production_agent():
    from atguigu_ai.agent.agent import Agent, AgentConfig

    return Agent.load(ECS_DEMO, config=AgentConfig())


async def _register_and_bind(
    client: httpx.AsyncClient,
    engine: Engine,
    verify_token: str,
) -> tuple[str, str]:
    register = await client.post(
        "/api/auth/register",
        json={"email": EVAL_EMAIL, "password": PASSWORD},
    )
    assert register.status_code == 202, register.text
    verify = await client.post("/api/auth/verify-email", json={"token": verify_token})
    assert verify.status_code == 200, verify.text

    with Session(engine) as session:
        account = session.execute(
            select(Account).where(Account.email_normalized == EVAL_EMAIL)
        ).scalar_one()
        account_id = account.account_id
        session.add(
            AccountUserBinding(
                account_id=account_id,
                user_id=EVAL_USER_ID,
                seed_version=AUTH_SEED_VERSION,
            )
        )
        session.commit()

    login = await client.post(
        "/api/auth/login",
        json={"email": EVAL_EMAIL, "password": PASSWORD},
    )
    assert login.status_code == 200, login.text
    return account_id, EVAL_USER_ID


def _patch_action_database(
    session_factory: sessionmaker[Session],
    engine: Engine,
) -> tuple[Any, Any, Any, Any]:
    import actions.db as action_db

    previous = (action_db, action_db.engine, action_db.SessionLocal, action_db.url)
    action_db.engine = engine
    action_db.SessionLocal = session_factory
    action_db.url = str(engine.url)
    return previous


def _seed_eval_business_data(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        _upsert_reference_data(session)
        _upsert_eval_orders(session)
        session.commit()


def _reset_eval_business_data(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        order_ids = [
            "eval-order-active",
            "eval-order-shipped",
            "eval-order-address",
            "eval-order-cancel",
            "eval-order-postsale",
        ]
        session.execute(
            text(
                "DELETE p FROM postsale p "
                "JOIN order_detail d ON d.order_detail_id = p.order_detail_id "
                "WHERE d.order_id IN :order_ids"
            ).bindparams(order_ids=tuple(order_ids))
        )
        session.execute(
            text("DELETE FROM order_logistics WHERE order_id IN :order_ids").bindparams(
                order_ids=tuple(order_ids)
            )
        )
        session.execute(
            text("DELETE FROM logistics WHERE logistics_id LIKE 'eval-logistics-%'")
        )
        session.execute(
            text("DELETE FROM order_detail WHERE order_id IN :order_ids").bindparams(
                order_ids=tuple(order_ids)
            )
        )
        session.execute(
            text("DELETE FROM order_info WHERE order_id IN :order_ids").bindparams(
                order_ids=tuple(order_ids)
            )
        )
        session.execute(
            text("DELETE FROM receive_info WHERE user_id=:user_id AND receive_id LIKE 'eval-receive-%'"),
            {"user_id": EVAL_USER_ID},
        )
        session.commit()
        _upsert_reference_data(session)
        _upsert_eval_orders(session)
        session.commit()


def _upsert_reference_data(session: Session) -> None:
    status_rows = [
        ("待支付", 100),
        ("已取消", 200),
        ("待发货", 310),
        ("已发货", 320),
        ("已签收", 330),
        ("售后中", 400),
        ("已完成", 900),
    ]
    for order_status, status_code in status_rows:
        if session.get(OrderStatus, order_status) is None:
            session.add(OrderStatus(order_status=order_status, status_code=status_code))

    postsale_rows = [
        ("退款待审核", 1, 0, 0, 410),
        ("退货待审核", 0, 1, 0, 420),
        ("换货待审核", 0, 0, 1, 430),
    ]
    for name, is_refund, is_return, is_exchange, status_code in postsale_rows:
        if session.get(PostsaleStatus, name) is None:
            session.add(
                PostsaleStatus(
                    postsale_status=name,
                    is_refund=is_refund,
                    is_return=is_return,
                    is_exchange=is_exchange,
                    status_code=status_code,
                )
            )

    for province, city, district in [
        (OLD_PROVINCE, OLD_CITY, OLD_DISTRICT),
        (NEW_PROVINCE, NEW_CITY, NEW_DISTRICT),
    ]:
        if session.get(Region, {"province": province, "city": city, "district": district}) is None:
            session.add(Region(province=province, city=city, district=district))

    if session.get(ProductCategory, "手机") is None:
        session.add(ProductCategory(product_category="手机"))
    if session.get(SkuInfo, "eval-sku-1") is None:
        session.add(
            SkuInfo(
                sku_id="eval-sku-1",
                sku_name="评测手机",
                sku_price=Decimal("1999.00"),
                sku_category="手机",
                sku_count=100,
            )
        )
    for reason in [POSTSALE_REASON_REFUND, POSTSALE_REASON_EXCHANGE]:
        if session.get(PostsaleReason, reason) is None:
            session.add(PostsaleReason(postsale_reason=reason, product_category=None))


def _upsert_eval_orders(session: Session) -> None:
    if session.get(UserInfo, EVAL_USER_ID) is None:
        session.add(UserInfo(user_id=EVAL_USER_ID))
        session.flush()

    now = datetime(2026, 7, 19, 12, 0, tzinfo=TZ)
    receive_ids = {
        "active": "eval-receive-active",
        "shipped": "eval-receive-shipped",
        "address": "eval-receive-address",
        "cancel": "eval-receive-cancel",
        "postsale": "eval-receive-postsale",
    }
    for key, receive_id in receive_ids.items():
        if session.get(ReceiveInfo, receive_id) is None:
            session.add(
                ReceiveInfo(
                    receive_id=receive_id,
                    user_id=EVAL_USER_ID,
                    receiver_name=f"评测用户{key}",
                    receiver_phone="13800000000",
                    receive_province=OLD_PROVINCE,
                    receive_city=OLD_CITY,
                    receive_district=OLD_DISTRICT,
                    receive_street_address=OLD_STREET,
                )
            )

    orders = [
        ("eval-order-active", "待发货", receive_ids["active"], None, None),
        ("eval-order-shipped", "已发货", receive_ids["shipped"], now - timedelta(hours=3), None),
        ("eval-order-address", "待发货", receive_ids["address"], None, None),
        ("eval-order-cancel", "待发货", receive_ids["cancel"], None, None),
        ("eval-order-postsale", "已签收", receive_ids["postsale"], now - timedelta(days=1), None),
    ]
    for order_id, status, receive_id, delivered_time, complete_time in orders:
        if session.get(OrderInfo, order_id) is None:
            session.add(
                OrderInfo(
                    order_id=order_id,
                    create_time=now - timedelta(days=2),
                    user_id=EVAL_USER_ID,
                    receive_id=receive_id,
                    order_status=status,
                    payment_time=now - timedelta(days=2, hours=-1),
                    delivered_time=delivered_time,
                    complete_time=complete_time,
                )
            )
    session.flush()

    details = [
        ("eval-detail-active", "eval-order-active"),
        ("eval-detail-shipped", "eval-order-shipped"),
        ("eval-detail-address", "eval-order-address"),
        ("eval-detail-cancel", "eval-order-cancel"),
        ("eval-detail-postsale", "eval-order-postsale"),
    ]
    for detail_id, order_id in details:
        if session.get(OrderDetail, detail_id) is None:
            session.add(
                OrderDetail(
                    order_detail_id=detail_id,
                    order_id=order_id,
                    sku_id="eval-sku-1",
                    sku_name="评测手机",
                    sku_count=1,
                    total_amount=Decimal("1999.00"),
                    final_amount=Decimal("1999.00"),
                    discount_amount=Decimal("0.00"),
                )
            )

    logistics_rows = [
        (
            "eval-logistics-shipped",
            "eval-order-shipped",
            "\n".join(
                [
                    "2026-07-19 08:00:00 上海分拨中心 已揽收",
                    "2026-07-19 11:20:00 杭州转运中心 运输中",
                ]
            ),
        ),
        (
            "eval-logistics-postsale",
            "eval-order-postsale",
            "\n".join(
                [
                    "2026-07-18 08:00:00 上海分拨中心 已揽收",
                    "2026-07-18 18:00:00 杭州西湖区 文三路 1 号 已签收",
                ]
            ),
        ),
    ]
    existing_links = {
        row[0]
        for row in session.execute(
            text("SELECT logistics_id FROM order_logistics WHERE order_id LIKE 'eval-order-%'")
        )
    }
    for logistics_id, order_id, tracking in logistics_rows:
        if session.get(Logistics, logistics_id) is None:
            session.add(
                Logistics(
                    logistics_id=logistics_id,
                    create_time=now - timedelta(days=1),
                    delivered_time=now if order_id == "eval-order-postsale" else None,
                    logistics_tracking=tracking,
                )
            )
            session.flush()
        if logistics_id not in existing_links:
            session.execute(
                text(
                    "INSERT INTO order_logistics (order_id, logistics_id) "
                    "VALUES (:order_id, :logistics_id)"
                ),
                {"order_id": order_id, "logistics_id": logistics_id},
            )


def _fixture_values(engine: Engine, user_id: str, fixture_key: str | None) -> dict[str, str]:
    mapping = {
        "active_order": "eval-order-active",
        "shipped_order": "eval-order-shipped",
        "modifiable_address_order": "eval-order-address",
        "cancelable_order": "eval-order-cancel",
        "postsale_eligible_order": "eval-order-postsale",
    }
    if fixture_key is None:
        return {}
    order_id = mapping[fixture_key]
    with Session(engine) as session:
        order = (
            session.query(OrderInfo)
            .filter(OrderInfo.order_id == order_id, OrderInfo.user_id == user_id)
            .first()
        )
        assert order is not None
        values = {
            "order_id": order.order_id,
            "order_status": order.order_status,
            "tracking_snippet": "",
        }
        if order.logistics:
            values["tracking_snippet"] = order.logistics[0].logistics_tracking.splitlines()[-1]
        return values


async def _run_case(
    context: EvalContext,
    case: ChatEvalCase,
    expected: dict[str, str],
) -> tuple[str, int]:
    rendered = tuple(message.format(**expected) for message in case.messages)
    if case.category in {"order_query", "logistics_query"}:
        return await _run_lookup_case(context, case, rendered, expected)
    if case.category == "order_cancel":
        return await _run_cancel_case(context, case, rendered, expected)
    if case.category == "address_modify":
        return await _run_address_case(context, case, rendered, expected)
    if case.category == "postsale_apply":
        return await _run_postsale_case(context, case, rendered, expected)
    return await _run_boundary_case(context, rendered)


async def _run_lookup_case(
    context: EvalContext,
    case: ChatEvalCase,
    rendered: tuple[str, ...],
    expected: dict[str, str],
) -> tuple[str, int]:
    responses: list[str] = []
    buttons: list[dict[str, Any]] = []
    turns = 0
    for message in rendered:
        reply = await _send_message(context.client, message)
        turns += 1
        responses.extend(_texts(reply))
        buttons = _buttons(reply)
    response_text = "\n".join(responses)
    if not _score_factual(case, response_text, context.engine, expected) and buttons:
        payload = _find_button_payload(buttons, expected["order_id"])
        if payload:
            reply = await _send_message(context.client, payload)
            turns += 1
            responses.extend(_texts(reply))
    return "\n".join(responses), turns


async def _run_cancel_case(
    context: EvalContext,
    _case: ChatEvalCase,
    rendered: tuple[str, ...],
    expected: dict[str, str],
) -> tuple[str, int]:
    responses: list[str] = []
    turns = 0
    reply = await _send_message(context.client, rendered[0])
    turns += 1
    responses.extend(_texts(reply))
    payload = _find_button_payload(_buttons(reply), expected["order_id"])
    if payload:
        reply = await _send_message(context.client, payload)
        turns += 1
        responses.extend(_texts(reply))
    confirm_payload = _find_button_payload(_buttons(reply), "if_cancel_order=true")
    if confirm_payload:
        reply = await _send_message(context.client, confirm_payload)
        turns += 1
        responses.extend(_texts(reply))
    elif len(rendered) > 1:
        reply = await _send_message(context.client, rendered[-1])
        turns += 1
        responses.extend(_texts(reply))
    return "\n".join(responses), turns


async def _run_address_case(
    context: EvalContext,
    case: ChatEvalCase,
    rendered: tuple[str, ...],
    expected: dict[str, str],
) -> tuple[str, int]:
    responses: list[str] = []
    turns = 0
    reply = await _send_message(context.client, rendered[0])
    turns += 1
    responses.extend(_texts(reply))
    payload = _find_button_payload(_buttons(reply), expected["order_id"])
    if payload:
        reply = await _send_message(context.client, payload)
        turns += 1
        responses.extend(_texts(reply))
    payload = _find_button_payload(_buttons(reply), "receive_id=modify")
    if payload:
        reply = await _send_message(context.client, payload)
        turns += 1
        responses.extend(_texts(reply))
    if case.case_id == "address_modify_direct":
        payload = _find_button_payload(_buttons(reply), "modify_content=收货地址")
        if payload:
            reply = await _send_message(context.client, payload)
            turns += 1
            responses.extend(_texts(reply))
        for token in [NEW_PROVINCE, NEW_CITY, NEW_DISTRICT]:
            payload = _find_button_payload(_buttons(reply), token)
            if payload:
                reply = await _send_message(context.client, payload)
                turns += 1
                responses.extend(_texts(reply))
        reply = await _send_message(context.client, NEW_STREET)
        turns += 1
        responses.extend(_texts(reply))
    else:
        payload = _find_button_payload(_buttons(reply), "modify_content=收货人姓名")
        if payload:
            reply = await _send_message(context.client, payload)
            turns += 1
            responses.extend(_texts(reply))
        reply = await _send_message(context.client, "张三评测")
        turns += 1
        responses.extend(_texts(reply))
    payload = _find_button_payload(_buttons(reply), "if_modify_continue=false")
    if payload:
        reply = await _send_message(context.client, payload)
        turns += 1
        responses.extend(_texts(reply))
    payload = _find_button_payload(_buttons(reply), "set_receive_info=true")
    if payload:
        reply = await _send_message(context.client, payload)
        turns += 1
        responses.extend(_texts(reply))
    return "\n".join(responses), turns


async def _run_postsale_case(
    context: EvalContext,
    case: ChatEvalCase,
    rendered: tuple[str, ...],
    expected: dict[str, str],
) -> tuple[str, int]:
    responses: list[str] = []
    turns = 0
    reply = await _send_message(context.client, rendered[0])
    turns += 1
    responses.extend(_texts(reply))
    buttons = _buttons(reply)
    payload = _find_button_payload(buttons, expected["order_id"])
    if payload:
        reply = await _send_message(context.client, payload)
        turns += 1
        responses.extend(_texts(reply))
    postsale_type = "退货" if case.case_id == "postsale_apply_refund" else "换货"
    reply = await _send_message(context.client, postsale_type)
    turns += 1
    responses.extend(_texts(reply))
    reply = await _send_message(context.client, rendered[2])
    turns += 1
    responses.extend(_texts(reply))
    return "\n".join(responses), turns


async def _run_boundary_case(
    context: EvalContext,
    rendered: tuple[str, ...],
) -> tuple[str, int]:
    responses: list[str] = []
    turns = 0
    for message in rendered:
        reply = await _send_message(context.client, message)
        turns += 1
        responses.extend(_texts(reply))
    return "\n".join(responses), turns


async def _send_message(client: httpx.AsyncClient, message: str) -> list[dict[str, Any]]:
    response = await client.post(
        "/api/chat/messages",
        headers={"X-CSRF-Token": client.cookies.get("auth_csrf")},
        json={"message": message},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert isinstance(payload, list)
    return payload


def _texts(payload: list[dict[str, Any]]) -> list[str]:
    return [item.get("text") or "" for item in payload]


def _buttons(payload: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buttons: list[dict[str, Any]] = []
    for item in payload:
        item_buttons = item.get("buttons")
        if isinstance(item_buttons, list):
            buttons.extend(button for button in item_buttons if isinstance(button, dict))
    return buttons


def _find_button_payload(buttons: list[dict[str, Any]], needle: str) -> str | None:
    for button in buttons:
        payload = str(button.get("payload") or "")
        title = str(button.get("title") or "")
        if needle in payload or needle in title:
            return payload
    return None


def _score_completion(
    case: ChatEvalCase,
    response_text: str,
    engine: Engine,
    expected: dict[str, str],
) -> bool:
    if case.expectation == "boundary":
        return _score_boundary(case, response_text)
    if case.expectation == "read":
        return _score_factual(case, response_text, engine, expected)
    formatted = [token.format(**expected) for token in case.expected_response_substrings]
    return _score_factual(case, response_text, engine, expected) and all(
        token in response_text for token in formatted
    )


def _score_factual(
    case: ChatEvalCase,
    response_text: str,
    engine: Engine,
    expected: dict[str, str],
) -> bool:
    if case.expectation == "boundary":
        return True
    formatted = [token.format(**expected) for token in case.expected_response_substrings]
    if case.expectation == "read":
        return all(token in response_text for token in formatted)

    with Session(engine) as session:
        order = session.get(OrderInfo, expected["order_id"])
        assert order is not None
        if case.expected_final_status is not None:
            return order.order_status == case.expected_final_status
        if case.expected_address_fragment is not None:
            receive = session.get(ReceiveInfo, order.receive_id)
            assert receive is not None
            full_address = (
                f"{receive.receive_province}"
                f"{receive.receive_city if receive.receive_city != receive.receive_province else ''}"
                f"{receive.receive_district}"
                f"{receive.receive_street_address}"
            )
            if case.case_id == "address_modify_name":
                return receive.receiver_name == case.expected_address_fragment
            return case.expected_address_fragment in full_address
        if case.expected_postsale_type is not None:
            rows = (
                session.query(Postsale)
                .join(OrderDetail, OrderDetail.order_detail_id == Postsale.order_detail_id)
                .filter(OrderDetail.order_id == expected["order_id"])
                .all()
            )
            if not rows:
                return False
            actual_types = {row.postsale_type for row in rows}
            acceptable = (
                {"退货", "退货退款"} if case.expected_postsale_type == "退货退款" else {case.expected_postsale_type}
            )
            return bool(actual_types & acceptable) and order.order_status == "售后中"
    return all(token in response_text for token in formatted)


def _score_boundary(case: ChatEvalCase, response_text: str) -> bool:
    return (
        all(token in response_text for token in case.expected_boundary_substrings)
        and all(token not in response_text for token in case.forbidden_response_substrings)
    )
