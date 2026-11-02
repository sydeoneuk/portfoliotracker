import base64
import datetime
import json
import unittest
from unittest.mock import patch

from sqlalchemy import select, text, delete
from starlette.testclient import TestClient
from itsdangerous import TimestampSigner

from tests import test_import_database as database_tests
from tests.test_listing_reconciliation import candidate
from app import reconciliation as service
from app.models import Position, Instrument, UserInstrumentMapping, InstrumentMappingProposal, InstrumentMappingAudit
from app.models.platform import Platform
from app.auth.models import User
from app.valuation import effective_price_sql


class FakeLookup:
    def __init__(self, candidates=None, fail=False):
        self.records = candidates or [candidate()]
        self.fail = fail

    def candidates(self, instrument, symbol=None):
        return [symbol] if symbol else [c["symbol"] for c in self.records]

    def inspect(self, symbol, expected_isin=None):
        if self.fail:
            raise RuntimeError("provider unavailable")
        return dict(next((c for c in self.records if c["symbol"] == symbol), {**self.records[0], "symbol": symbol}))


@unittest.skipUnless(database_tests.os.environ.get("TEST_DATABASE_URL"), "requires disposable PostgreSQL TEST_DATABASE_URL")
class TestReconciliationDatabase(unittest.TestCase):
    setUpClass = classmethod(database_tests.TestImportDatabase.setUpClass.__func__)
    tearDownClass = classmethod(database_tests.TestImportDatabase.tearDownClass.__func__)
    cleanup_records = database_tests.TestImportDatabase.cleanup_records

    def setUp(self):
        database_tests.TestImportDatabase.setUp(self)
        instrument = self.db.get(Instrument, self.ticker)
        instrument.isin, instrument.yf_ticker, instrument.exchange = "GB00BP6MXD84", "WRONG", "NYSE"
        self.db.add(Position(user_id=self.user_id, platform_id=self.platform_id, ticker=self.ticker,
                             quantity=2, average_price=12, current_price=None))
        self.db.commit()

    def proposal(self, **kwargs):
        return service.propose(self.db, self.user_id, self.ticker, lookup=kwargs.pop("lookup", FakeLookup()), **kwargs)[0]

    def metadata_proposal(self, **kwargs):
        return service.propose_metadata(self.db, self.user_id, self.ticker, 'SHEL.L',
            kwargs.pop('isin', 'GB00BP6MXD84'), kwargs.pop('currency', 'GBP'),
            'Broker statement 2026-11-01', lookup=kwargs.pop('lookup', FakeLookup()), **kwargs)[0]

    def test_missing_currency_metadata_preview_apply_and_undo_preserve_original_records(self):
        instrument = self.db.get(Instrument, self.ticker)
        instrument.currency_code = None
        self.db.commit()
        proposal = self.metadata_proposal()
        self.assertEqual(proposal.status, 'verified')
        self.assertIsNone(instrument.currency_code)  # preview is read-only for metadata
        mapping, audit = service.apply_proposal(self.db, self.user_id, proposal.id)
        self.assertEqual((instrument.currency_code, instrument.yf_ticker), ('GBP', 'SHEL.L'))
        self.assertEqual(self.price(), 20)
        position = self.db.get(Position, (self.ticker, self.platform_id, self.user_id))
        self.assertEqual((position.quantity, position.average_price), (2, 12))
        self.assertIsNone(position.cost_basis_amount)
        self.assertIsNone(audit.after['metadata_correction']['before']['currency_code'])
        service.undo_change(self.db, self.user_id, audit.id)
        self.assertEqual((instrument.currency_code, instrument.yf_ticker), (None, 'WRONG'))
        self.assertIsNone(service.mapping_for(self.db, self.user_id, self.ticker))

    def test_isin_correction_requires_new_identity_match_and_blocks_stale_quotes_after_undo(self):
        instrument = self.db.get(Instrument, self.ticker)
        instrument.isin = 'US0000000001'
        self.db.commit()
        proposal = self.metadata_proposal()
        mapping, audit = service.apply_proposal(self.db, self.user_id, proposal.id)
        self.assertEqual(instrument.isin, 'GB00BP6MXD84')
        service.undo_change(self.db, self.user_id, audit.id)
        self.assertEqual(instrument.isin, 'US0000000001')
        position = self.db.get(Position, (self.ticker, self.platform_id, self.user_id))
        position.current_price = 999
        self.db.commit()
        self.assertIsNone(self.price())

    def test_metadata_conflict_and_existing_currency_cannot_be_bypassed(self):
        with self.assertRaises(ValueError):
            self.metadata_proposal(currency='USD')
        instrument = self.db.get(Instrument, self.ticker)
        instrument.currency_code = None
        self.db.commit()
        proposal = self.metadata_proposal(lookup=FakeLookup([candidate(isin='US0000000001')]))
        self.assertEqual(proposal.status, 'needs_review')
        with self.assertRaises(service.ReconciliationConflict):
            service.apply_proposal(self.db, self.user_id, proposal.id)
        self.db.rollback()
        self.assertIsNone(instrument.currency_code)

    def test_metadata_endpoints_require_admin_including_apply_and_undo(self):
        from app.web import server
        client = self.client(self.user_id)
        payload = dict(ticker=self.ticker, symbol='SHEL.L', isin='GB00BP6MXD84',
                       currency='GBP', statement_reference='Broker statement 2026-11-01')
        self.assertEqual(client.post('/admin/reconciliation/metadata/propose', json=payload).status_code, 403)
        self.assertEqual(client.get('/admin/instruments/reconciliation', follow_redirects=False).status_code, 302)
        instrument = self.db.get(Instrument, self.ticker)
        instrument.currency_code = None
        self.db.commit()
        with patch.object(server, '_is_admin', return_value=True):
            self.assertEqual(client.get('/admin/instruments/reconciliation').status_code, 200)
        # Router receives the original callable, so patch its configuration.
        from app.config import settings
        with patch.object(type(settings), 'admin_email_set', new_callable=unittest.mock.PropertyMock,
                          return_value={self.db.get(User, self.user_id).email.lower()}), \
             patch('app.reconciliation.YahooListingLookup', return_value=FakeLookup()):
            response = client.post('/admin/reconciliation/metadata/propose', json=payload)
            self.assertEqual(response.status_code, 200, response.text)
            proposal_id = response.json()['proposals'][0]['id']
            with patch.object(type(settings), 'admin_email_set', new_callable=unittest.mock.PropertyMock, return_value=set()):
                self.assertEqual(client.post('/settings/reconciliation/apply', json={'proposal_id': proposal_id}).status_code, 403)
            result = client.post('/settings/reconciliation/apply', json={'proposal_id': proposal_id})
            self.assertEqual(result.status_code, 200, result.text)
            audit_id = result.json()['audit_id']
        self.assertEqual(client.post('/settings/reconciliation/undo', json={'audit_id': audit_id}).status_code, 403)

    def test_metadata_undo_refuses_to_overwrite_newer_catalogue_edits(self):
        instrument = self.db.get(Instrument, self.ticker)
        instrument.currency_code = None
        self.db.commit()
        proposal = self.metadata_proposal()
        mapping, audit = service.apply_proposal(self.db, self.user_id, proposal.id)
        instrument.isin = 'US0000000001'
        self.db.commit()
        with self.assertRaises(service.ReconciliationConflict):
            service.undo_change(self.db, self.user_id, audit.id)
        self.db.rollback()
        self.assertEqual(instrument.isin, 'US0000000001')

    def price(self, user_id=None):
        return self.db.execute(text(f"SELECT {effective_price_sql()} FROM positions p WHERE p.user_id=:uid AND p.ticker=:ticker"),
                               {"uid": user_id or self.user_id, "ticker": self.ticker}).scalar_one()

    def test_apply_uses_verified_quote_without_touching_legacy_identity_cost_or_quantity(self):
        proposal = self.proposal()
        self.assertEqual(proposal.status, "verified")
        self.assertIsNone(self.price())
        mapping, audit = service.apply_proposal(self.db, self.user_id, proposal.id)
        self.assertEqual(self.price(), 20)  # 2000 GBX → recorded GBP units
        position = self.db.get(Position, (self.ticker, self.platform_id, self.user_id))
        instrument = self.db.get(Instrument, self.ticker)
        self.assertEqual((position.quantity, position.average_price, position.current_price), (2, 12, None))
        self.assertEqual((instrument.yf_ticker, instrument.exchange, instrument.currency_code), ("WRONG", "NYSE", "GBP"))
        self.assertEqual(audit.after["provider_symbol"], "SHEL.L")
        service.undo_change(self.db, self.user_id, audit.id)
        self.assertIsNone(self.price())
        self.assertEqual(self.db.query(InstrumentMappingAudit).filter_by(user_id=self.user_id).count(), 2)

    def test_expired_and_changed_baselines_cannot_be_applied(self):
        proposal = self.proposal()
        proposal.expires_at = datetime.datetime.utcnow() - datetime.timedelta(seconds=1)
        self.db.commit()
        with self.assertRaises(service.ReconciliationConflict):
            service.apply_proposal(self.db, self.user_id, proposal.id)
        self.db.rollback()
        proposal = self.proposal()
        self.db.get(Instrument, self.ticker).isin = "US0000000001"
        self.db.commit()
        with self.assertRaises(service.ReconciliationConflict):
            service.apply_proposal(self.db, self.user_id, proposal.id)

    def test_ambiguous_candidates_require_an_explicit_listing_choice(self):
        lookup = FakeLookup([candidate(), candidate("OTHER.L")])
        proposals = service.propose(self.db, self.user_id, self.ticker, lookup=lookup)
        self.assertEqual({p.status for p in proposals}, {"ambiguous"})
        with self.assertRaises(service.ReconciliationConflict):
            service.apply_proposal(self.db, self.user_id, proposals[0].id)
        self.db.rollback()
        proposal = self.proposal(symbol="SHEL.L", lookup=lookup)
        self.assertEqual(proposal.status, "verified")

    def test_currency_conversion_requires_explicit_choice_and_verified_fx(self):
        lookup = FakeLookup([candidate("US", currency="USD", exchange="NYSE", mic="XNYS", price="100")])
        proposal = self.proposal(symbol="US", lookup=lookup)
        self.assertEqual(proposal.status, "needs_review")
        with patch("app.reconciliation.get_fx_rates_to_gbp", return_value={"USD": 0.8}):
            proposal = self.proposal(symbol="US", lookup=lookup, allow_currency_conversion=True)
        mapping, _ = service.apply_proposal(self.db, self.user_id, proposal.id)
        self.assertEqual((mapping.price, mapping.quote_currency, mapping.normalized_price), (100, "USD", 80))
        self.assertEqual(self.price(), 80)

    def test_missing_fx_and_identity_mismatch_remain_unapplied(self):
        lookup = FakeLookup([candidate("US", currency="USD")])
        with patch("app.reconciliation.get_fx_rates_to_gbp", return_value={}):
            proposal = self.proposal(symbol="US", lookup=lookup, allow_currency_conversion=True)
        self.assertEqual(proposal.status, "needs_review")
        with self.assertRaises(service.ReconciliationConflict):
            service.apply_proposal(self.db, self.user_id, proposal.id)
        self.db.rollback()
        proposal = self.proposal(lookup=FakeLookup([candidate(isin="US0000000001")]))
        self.assertEqual(proposal.status, "needs_review")

    def test_undo_cannot_overwrite_a_newer_correction(self):
        first, first_audit = service.apply_proposal(self.db, self.user_id, self.proposal().id)
        second_proposal = self.proposal(lookup=FakeLookup([candidate("SECOND.L", price="3000")]))
        _, second_audit = service.apply_proposal(self.db, self.user_id, second_proposal.id)
        with self.assertRaises(service.ReconciliationConflict):
            service.undo_change(self.db, self.user_id, first_audit.id)
        self.db.rollback()
        service.undo_change(self.db, self.user_id, second_audit.id)
        self.assertEqual(self.price(), 20)

    def test_provider_failure_retains_mapping_and_previous_quote(self):
        mapping, _ = service.apply_proposal(self.db, self.user_id, self.proposal().id)
        with self.assertRaises(RuntimeError):
            service.refresh_mapping(self.db, self.user_id, mapping.id, lookup=FakeLookup(fail=True))
        self.db.rollback()
        self.assertEqual(self.price(), 20)
        service.refresh_mapping(self.db, self.user_id, mapping.id, lookup=FakeLookup([candidate(price="2500")]))
        self.assertEqual(self.price(), 25)

    def client(self, user_id=None):
        from app.web import server
        from app.config import settings
        server.app.dependency_overrides[server.get_session] = lambda: self.db
        self.addCleanup(server.app.dependency_overrides.clear)
        client = TestClient(server.app)
        self.addCleanup(client.close)
        if user_id is not None:
            cookie = TimestampSigner(settings.session_secret).sign(base64.b64encode(json.dumps({"user_id": user_id}).encode())).decode()
            client.cookies.set("session", cookie)
        return client

    def test_settings_and_endpoints_expose_review_apply_history_and_undo(self):
        from app.web import server
        client = self.client(self.user_id)
        with patch.object(server, "_get_server_ip", return_value=None):
            response = client.get("/settings")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Listing reconciliation", response.text)
        self.assertEqual(client.get("/static/reconciliation.js").status_code, 200)
        listing = client.get("/settings/reconciliation").json()
        self.assertEqual(listing["items"][0]["ticker"], self.ticker)
        with patch("app.reconciliation.YahooListingLookup", return_value=FakeLookup()):
            result = client.post("/settings/reconciliation/propose", json={"ticker": self.ticker})
        self.assertEqual(result.status_code, 200)
        proposal_id = result.json()["proposals"][0]["id"]
        result = client.post("/settings/reconciliation/apply", json={"proposal_id": proposal_id})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(client.get("/settings/reconciliation").json()["total"], 0)
        history = client.get("/settings/reconciliation/history").json()["items"]
        self.assertTrue(history[0]["can_undo"])
        self.assertEqual(client.post("/settings/reconciliation/undo", json={"audit_id": history[0]["id"]}).status_code, 200)
        self.assertEqual(client.get("/settings/reconciliation").json()["total"], 1)

    def test_authentication_cross_origin_and_unheld_tickers_are_rejected(self):
        self.assertEqual(self.client().get("/settings/reconciliation").status_code, 401)
        client = self.client(self.user_id)
        self.assertEqual(client.post("/settings/reconciliation/propose", json={"ticker": self.ticker},
                                     headers={"Origin": "https://unrelated.invalid"}).status_code, 403)
        self.assertEqual(client.post("/settings/reconciliation/propose", json={"ticker": "NOT_HELD"}).status_code, 404)

    def test_private_mapping_and_audit_are_not_visible_to_another_user(self):
        other = User(email=self.ticker + "other@example.invalid", provider="test", provider_id="other")
        self.db.add(other)
        self.db.flush()
        other_id = other.id
        platform = Platform(user_id=other_id, name="Other", platform_type="t212_api", account_type="ISA", currency="GBP")
        self.db.add(platform)
        self.db.flush()
        self.db.add(Position(user_id=other_id, platform_id=platform.id, ticker=self.ticker, quantity=1, current_price=33))
        self.db.commit()
        def cleanup_other():
            self.db.rollback()
            self.db.execute(delete(Position).where(Position.user_id == other_id))
            self.db.execute(delete(Platform).where(Platform.user_id == other_id))
            self.db.execute(delete(User).where(User.id == other_id))
            self.db.commit()
        self.addCleanup(cleanup_other)
        with self.assertRaises(service.ReconciliationConflict):
            service.require_exclusive_instrument(self.db, self.user_id, self.ticker)
        mapping, audit = service.apply_proposal(self.db, self.user_id, self.proposal().id)
        self.assertEqual(self.price(), 20)
        self.assertEqual(self.price(other_id), 33)
        client = self.client(other_id)
        item = client.get("/settings/reconciliation?missing_only=false").json()["items"][0]
        self.assertIsNone(item["mapping"])
        self.assertEqual(client.get("/settings/reconciliation/history").json()["items"], [])
        self.assertEqual(client.post("/settings/reconciliation/undo", json={"audit_id": audit.id}).status_code, 404)
        self.assertEqual(client.post("/settings/reconciliation/refresh", json={"mapping_id": mapping.id}).status_code, 404)
