import os
import tempfile
import unittest
from datetime import datetime, timezone

database_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
database_file.close()
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "StrongPass123")
os.environ["DATABASE_URL"] = f"sqlite:///{database_file.name}"

from app import (
    app,
    db,
    User,
    AuditLog,
    Transaction,
    GroupChat,
    GroupMembership,
    GroupMessage,
    GroupMessageReaction,
    LedgerEntry,
    Notification,
    ScheduledTransfer,
    Beneficiary,
    SupportTicket,
    BillPayment,
    BillPaymentLedgerEntry,
    PrivilegedTransferRequest,
    CheckDeposit,
    UserRole,
    UserPermission,
    DeveloperApiKey,
    format_singapore_time,
    parse_datetime,
)


class MiniBankSmokeTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        with app.app_context():
            db.drop_all()
            db.create_all()
            admin = User(
                username="admin",
                display_name="Administrator",
                balance="100.00",
                is_admin=True,
                account_type="admin",
                is_enabled=True,
            )
            admin.set_password("StrongPass123")
            db.session.add(admin)
            db.session.commit()
        self.client = app.test_client()

    def tearDown(self):
        with app.app_context():
            db.drop_all()

    def login(self, username, password):
        return self.client.post(
            "/login",
            data={"username": username, "password": password},
            follow_redirects=False,
        )

    def test_public_and_login_pages_work(self):
        public_page = self.client.get("/")
        self.assertEqual(public_page.status_code, 200)
        self.assertIn(
            b"Copyright Pranav Hemahlathaa Harish and Hari Suhanth Karthikeyan, 2026.",
            public_page.data,
        )
        self.assertIn(b"SGT / Asia/Singapore (UTC+8)", public_page.data)
        self.assertEqual(
            format_singapore_time(
                datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
            ),
            "01 Jan 2026, 08:00 SGT",
        )
        self.assertEqual(
            parse_datetime("2026-01-01T08:00").hour,
            0,
        )
        response = self.login("admin", "StrongPass123")
        self.assertEqual(response.status_code, 302)
        dashboard = self.client.get("/dashboard")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn(
            b"Copyright Pranav Hemahlathaa Harish and Hari Suhanth Karthikeyan, 2026.",
            dashboard.data,
        )
        self.assertIn(b"Account active", dashboard.data)
        self.assertIn(b"SGT / Asia/Singapore (UTC+8)", dashboard.data)
        self.assertIn(b"role-badge-admin", dashboard.data)
        self.assertIn(b"winged", dashboard.data)
        accounts = self.client.get("/accounts")
        self.assertEqual(accounts.status_code, 200)
        self.assertIn(b"Checking account", accounts.data)
        self.assertNotIn(b"Savings", accounts.data)
        self.assertEqual(self.client.get("/accounts/move-money").status_code, 404)

    def test_developer_tools_are_developer_only(self):
        self.login("admin", "StrongPass123")
        self.assertEqual(self.client.get("/developer-tools").status_code, 403)
        self.create_user("developer", account_type="developer")
        self.client.post("/logout")
        with app.app_context():
            developer = User.query.filter_by(username="developer").one()
            db.session.commit()
        self.login("developer", "Password123")
        page = self.client.get("/developer-tools")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Developer tools", page.data)
        response = self.client.post(
            "/developer-tools",
            data={"action": "create_api_key", "name": "Local test"},
            follow_redirects=True,
        )
        self.assertIn(b"Copy this API key now", response.data)
        with app.app_context():
            self.assertEqual(DeveloperApiKey.query.count(), 1)

    def test_management_role_can_create_user_and_transfer(self):
        self.login("admin", "StrongPass123")
        response = self.client.post(
            "/admin/create-user",
            data={
                "username": "dev",
                "display_name": "Developer",
                "password": "Developer123",
                "balance": "50.00",
                "account_type": "developer",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.client.post("/logout")
        self.login("dev", "Developer123")
        self.assertEqual(self.client.get("/admin").status_code, 200)
        self.assertIn(b"role-badge-developer", self.client.get("/admin").data)
        response = self.client.post(
            "/transfer",
            data={
                "receiver_username": "admin",
                "amount": "10.00",
                "note": "Smoke test",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/receipt/", response.headers["Location"])
        dashboard = self.client.get("/dashboard")
        self.assertIn(b"Reference: TXN-", dashboard.data)
        statement = self.client.get("/statements.csv")
        self.assertIn(b"SGT", statement.data)

    def create_user(self, username, account_type="standard", balance="50.00"):
        response = self.client.post(
            "/admin/create-user",
            data={
                "username": username,
                "display_name": username.title(),
                "password": "Password123",
                "balance": balance,
                "account_type": account_type,
            },
        )
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            return User.query.filter_by(username=username).one().id

    def test_impersonation_is_labeled_and_restorable(self):
        self.login("admin", "StrongPass123")
        user_id = self.create_user("impersonated")
        response = self.client.post(
            f"/admin/impersonate/{user_id}",
            data={"reason": "Smoke test support"},
        )
        self.assertEqual(response.status_code, 302)
        dashboard = self.client.get("/dashboard")
        self.assertIn(b"Impersonating", dashboard.data)
        with self.client.session_transaction() as flask_session:
            self.assertEqual(flask_session["impersonator_id"], 1)
        response = self.client.post("/admin/stop-impersonation")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get("/admin").status_code, 200)
        with app.app_context():
            self.assertEqual(
                AuditLog.query.filter_by(action="impersonation_started").count(), 1
            )
            self.assertEqual(
                AuditLog.query.filter_by(action="impersonation_ended").count(), 1
            )
            actor_id = User.query.filter_by(username="admin").one().id
            start_log = AuditLog.query.filter_by(
                action="impersonation_started"
            ).one()
            end_log = AuditLog.query.filter_by(
                action="impersonation_ended"
            ).one()
            self.assertEqual(start_log.actor_id, actor_id)
            self.assertEqual(end_log.actor_id, actor_id)

    def test_developer_impersonation_cannot_escalate_management_access(self):
        self.login("admin", "StrongPass123")
        self.create_user("developer", account_type="developer")
        target_id = self.create_user("developer-target")
        self.client.post("/logout")
        self.login("developer", "Password123")

        response = self.client.post(
            f"/admin/impersonate/{target_id}",
            data={"reason": "Developer support"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get("/admin").status_code, 403)
        self.assertIn(b"Impersonating", self.client.get("/dashboard").data)

        self.assertEqual(self.client.post("/admin/stop-impersonation").status_code, 302)
        self.assertEqual(self.client.get("/admin").status_code, 200)

    def test_privileged_transfer_validates_and_audits_atomically(self):
        self.login("admin", "StrongPass123")
        source_id = self.create_user("source", balance="100.00")
        receiver_id = self.create_user("receiver", balance="5.00")
        invalid = self.client.post(
            "/admin/transfer",
            data={
                "source_username": "source",
                "receiver_username": "receiver",
                "amount": "-1",
                "reason": "bad",
            },
            follow_redirects=True,
        )
        self.assertIn(b"greater than zero", invalid.data)
        missing_reason = self.client.post(
            "/admin/privileged-transfer",
            data={
                "from_username": "source",
                "to_username": "receiver",
                "amount": "10",
            },
            follow_redirects=True,
        )
        self.assertIn(b"reason is required", missing_reason.data)
        response = self.client.post(
            "/admin/transfer",
            data={
                "source_username": "source",
                "receiver_username": "receiver",
                "amount": "10.00",
                "reason": "Customer service correction",
            },
        )
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            self.assertEqual(str(db.session.get(User, source_id).balance), "90.00")
            self.assertEqual(str(db.session.get(User, receiver_id).balance), "15.00")
            self.assertEqual(
                AuditLog.query.filter_by(action="privileged_transfer").count(), 1
            )

    def test_privileged_transfer_approval_workflow(self):
        self.login("admin", "StrongPass123")
        source_id = self.create_user("approval-source", balance="40.00")
        receiver_id = self.create_user("approval-receiver", balance="1.00")
        response = self.client.post(
            "/admin/privileged-transfer/request",
            data={
                "source_username": "approval-source",
                "receiver_username": "approval-receiver",
                "amount": "7.00",
                "reason": "Approval smoke",
            },
        )
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            pending = PrivilegedTransferRequest.query.filter_by(
                status="pending"
            ).one()
            request_id = pending.id
        self.assertEqual(
            self.client.post(
                f"/admin/privileged-transfer/{request_id}/approve"
            ).status_code,
            302,
        )
        with app.app_context():
            self.assertEqual(
                str(db.session.get(User, source_id).balance), "33.00"
            )
            self.assertEqual(
                str(db.session.get(User, receiver_id).balance), "8.00"
            )
            self.assertEqual(
                PrivilegedTransferRequest.query.filter_by(status="approved").count(),
                1,
            )

    def test_group_chat_membership_posting_and_unread_state(self):
        self.login("admin", "StrongPass123")
        member_id = self.create_user("member")
        self.client.post("/logout")
        self.login("admin", "StrongPass123")
        response = self.client.post("/groups", data={"name": "Smoke group"})
        self.assertEqual(response.status_code, 302)
        group_id = int(response.headers["Location"].rsplit("/", 1)[-1])
        response = self.client.post(
            f"/groups/{group_id}/members",
            data={"username": "member", "action": "add"},
        )
        self.assertEqual(response.status_code, 302)
        self.client.post("/groups/%s" % group_id, data={"content": "Hello group"})
        self.client.post("/logout")
        self.login("member", "Password123")
        messages = self.client.get("/messages")
        self.assertIn(b"Smoke group", messages.data)
        self.assertGreaterEqual(
            messages.data.count(b"unread-badge"), 1
        )
        self.assertEqual(self.client.get(f"/groups/{group_id}").status_code, 200)
        with app.app_context():
            group_message = GroupMessage.query.filter_by(group_id=group_id).one()
            group_message_id = group_message.id
        self.assertEqual(
            self.client.post(
                f"/groups/messages/{group_message_id}/react",
                data={"emoji": "👍"},
            ).status_code,
            302,
        )
        self.assertEqual(
            self.client.get("/messages").data.count(b"unread-badge"), 0
        )
        with app.app_context():
            self.assertEqual(GroupChat.query.filter_by(name="Smoke group").count(), 1)
            self.assertEqual(
                GroupMembership.query.filter_by(user_id=member_id).count(), 1
            )
            self.assertEqual(
                GroupMessage.query.filter_by(group_id=group_id).count(), 1
            )
            self.assertEqual(
                GroupMessageReaction.query.filter_by(
                    group_message_id=group_message_id
                ).count(),
                1,
            )

    def test_local_roadmap_mvp_routes_and_ledger(self):
        self.login("admin", "StrongPass123")
        recipient_id = self.create_user("roadmap-recipient", balance="10.00")
        self.assertEqual(
            self.client.post(
                f"/admin/permissions/{recipient_id}",
                data={"permission": "audit_read"},
            ).status_code,
            302,
        )
        self.client.post("/logout")
        self.login("roadmap-recipient", "Password123")
        self.assertEqual(self.client.get("/admin/audit-logs").status_code, 200)
        self.client.post("/logout")
        self.login("admin", "StrongPass123")
        for path in (
            "/admin/audit-logs",
            "/beneficiaries",
            "/scheduled-transfers",
        ):
            self.assertEqual(self.client.get(path).status_code, 200, path)
        self.assertEqual(self.client.get("/statements").status_code, 200)
        self.assertEqual(self.client.get("/statements.csv").status_code, 200)
        self.assertEqual(self.client.get("/statements.pdf").status_code, 200)
        self.assertEqual(
            self.client.post(
                "/beneficiaries",
                data={"username": "roadmap-recipient", "nickname": "Recipient"},
            ).status_code,
            302,
        )
        self.assertEqual(
            self.client.post(
                "/scheduled-transfers",
                data={
                    "receiver_username": "roadmap-recipient",
                    "amount": "2.00",
                    "interval_days": "7",
                },
            ).status_code,
            302,
        )
        self.assertEqual(self.client.get("/notifications").status_code, 200)
        with app.app_context():
            self.assertEqual(Beneficiary.query.count(), 1)
            self.assertEqual(ScheduledTransfer.query.count(), 1)
            self.assertGreaterEqual(Notification.query.count(), 0)

        response = self.client.post(
            "/transfer",
            data={
                "receiver_username": "roadmap-recipient",
                "amount": "3.00",
                "note": "Ledger smoke",
            },
        )
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            self.assertEqual(LedgerEntry.query.count(), 2)
            self.assertEqual(
                PrivilegedTransferRequest.query.filter_by(status="pending").count(),
                0,
            )
            self.assertEqual(
                UserPermission.query.filter_by(
                    user_id=recipient_id,
                    permission="audit_read",
                ).count(),
                1,
            )

    def test_transfer_cancellation_window_restores_balances(self):
        self.login("admin", "StrongPass123")
        source_id = self.create_user("cancel-source", balance="20.00")
        receiver_id = self.create_user("cancel-receiver", balance="2.00")
        response = self.client.post(
            "/transfer",
            data={
                "receiver_username": "cancel-receiver",
                "amount": "4.00",
                "note": "Cancelable smoke",
            },
        )
        transaction_id = int(response.headers["Location"].rsplit("/", 1)[-1])
        self.assertEqual(
            self.client.post(f"/transfer/{transaction_id}/cancel").status_code,
            302,
        )
        with app.app_context():
            self.assertEqual(str(db.session.get(User, source_id).balance), "20.00")
            self.assertEqual(str(db.session.get(User, receiver_id).balance), "2.00")
            self.assertEqual(db.session.get(Transaction, transaction_id).status, "cancelled")

    def test_unlimited_transfers_and_duplicate_protection(self):
        self.login("admin", "StrongPass123")
        self.create_user("velocity-source", balance="10000.00")
        self.create_user("velocity-receiver", balance="0.00")
        self.client.post("/logout")
        self.login("velocity-source", "Password123")

        large_transfer = self.client.post(
            "/transfer",
            data={
                "receiver_username": "velocity-receiver",
                "amount": "5001.00",
                "note": "No transfer cap",
            },
        )
        self.assertEqual(large_transfer.status_code, 302)

        first = self.client.post(
            "/transfer",
            data={
                "receiver_username": "velocity-receiver",
                "amount": "100.00",
                "note": "Duplicate check",
            },
        )
        self.assertEqual(first.status_code, 302)
        duplicate = self.client.post(
            "/transfer",
            data={
                "receiver_username": "velocity-receiver",
                "amount": "100.00",
                "note": "Duplicate check",
            },
            follow_redirects=True,
        )
        self.assertIn(b"matching transfer", duplicate.data)

    def test_composable_roles_management_balances_and_demo_check(self):
        self.login("admin", "StrongPass123")
        source_id = self.create_user("check-writer", balance="100.00")
        recipient_id = self.create_user("check-recipient", balance="10.00")
        both = self.client.post(
            "/admin/create-user",
            data={
                "username": "dual-role",
                "display_name": "Dual Role",
                "password": "Password123",
                "balance": "20.00",
                "account_type": "standard",
                "role": ["admin", "developer"],
            },
        )
        self.assertEqual(both.status_code, 302)
        with app.app_context():
            dual = User.query.filter_by(username="dual-role").one()
            self.assertEqual(dual.account_type, "admin")
            self.assertTrue(dual.is_admin)
            self.assertEqual(
                {item.role for item in UserRole.query.filter_by(user_id=dual.id)},
                {"admin", "developer"},
            )
        admin_page = self.client.get("/admin")
        self.assertIn(b"role-badge-admin", admin_page.data)
        self.assertIn(b"role-badge-developer", admin_page.data)
        self.client.post("/logout")
        self.login("dual-role", "Password123")
        profile = self.client.get("/profile")
        self.assertIn(b"role-badge-admin", profile.data)
        self.assertIn(b"role-badge-developer", profile.data)
        self.client.post("/logout")
        self.login("admin", "StrongPass123")

        balances = self.client.get("/admin/balances")
        self.assertEqual(balances.status_code, 200)
        self.assertIn(b"Checking", balances.data)
        self.assertNotIn(b"Savings", balances.data)
        self.assertEqual(
            self.client.post(
                "/admin/check-deposit",
                data={
                    "source_username": "check-writer",
                    "recipient_username": "check-writer",
                    "amount": "10.00",
                    "check_number": "SELF-1",
                    "reference": "Invalid self check",
                },
                follow_redirects=True,
            ).status_code,
            200,
        )
        valid = self.client.post(
            "/admin/check-deposit",
            data={
                "source_username": "check-writer",
                "recipient_username": "check-recipient",
                "amount": "25.00",
                "check_number": "CHK-1001",
                "reference": "Rent reimbursement",
            },
        )
        self.assertEqual(valid.status_code, 302)
        with app.app_context():
            self.assertEqual(str(db.session.get(User, source_id).balance), "75.00")
            self.assertEqual(str(db.session.get(User, recipient_id).balance), "35.00")
            self.assertEqual(CheckDeposit.query.count(), 1)
            self.assertEqual(
                AuditLog.query.filter_by(action="demo_check_deposited").count(),
                1,
            )

        self.create_user("same-permissions-developer", account_type="developer")
        self.client.post("/logout")
        self.login("same-permissions-developer", "Password123")
        self.assertEqual(self.client.get("/admin/balances").status_code, 200)


def tearDownModule():
    with app.app_context():
        db.drop_all()
        db.engine.dispose()
    os.unlink(database_file.name)


if __name__ == "__main__":
    unittest.main()
