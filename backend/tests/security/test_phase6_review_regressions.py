"""Regressions for the Phase 6 independent review (docs/admin-user-workspace.md#defects).

F1 (P1): a page cursor positioned by a private sort value (a lead's name, a deal's amount)
    re-read its boundary row outside the caller's scope, so replaying someone else's cursor
    while moving one's own record across the boundary binary-searched the hidden value.
F2 (P2): reopening or restoring a closed deal its closer kept revealed whether the lead's new
    owner had archived the lead.
F3 (P3): restoring archived open work gave current work back to a deactivated owner, which
    creating and reopening refuse.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.utils import timezone

from arkray.core.keyset import INVALID_CURSOR
from arkray.identity import services as identity_services
from arkray.pipeline.models import Opportunity
from tests.factories import (
    AdminFactory,
    LeadFactory,
    MeetingFactory,
    OpportunityFactory,
    TaskFactory,
    default_stage,
)
from tests.helpers import signed_in

pytestmark = pytest.mark.django_db

API = "/api/v1/workspaces"
ME = f"{API}/me"


def is_invalid_cursor(response) -> bool:
    return bool(
        response.status_code == 400 and response.json()["error"]["message"] == INVALID_CURSOR
    )


# --- F1 ---------------------------------------------------------------------------------------
class TestCursorsNeverMeasureHiddenRows:
    def test_someone_elses_amount_cursor_is_refused_whatever_the_probe(self, user_a, user_b):
        """Priya's `next` link (leaked through a URL) replayed by Rahul: refused outright, so
        moving his own deal around the boundary measures nothing."""
        rahul, priya = signed_in(user_a), signed_in(user_b)
        OpportunityFactory(lead=LeadFactory(owner=user_b), value=Decimal("2345678.90"))
        OpportunityFactory(lead=LeadFactory(owner=user_b), value=Decimal("10.00"))
        leaked = priya.get(f"{ME}/opportunities", {"ordering": "-value", "page_size": 1}).json()
        probe = OpportunityFactory(lead=LeadFactory(owner=user_a), value=Decimal("1.00"))
        for value in ("1.00", "2345678.89", "2345678.91", "99999999.00"):
            Opportunity.objects.filter(pk=probe.pk).update(value=Decimal(value))
            assert is_invalid_cursor(rahul.get(leaked["next"])), value

    @pytest.mark.parametrize(
        ("ordering", "list_path"), [("-value", "opportunities"), ("name", "leads")]
    )
    def test_a_harvested_cursor_dies_when_its_row_moves_to_another_workspace(
        self, user_a, user_b, admin, ordering, list_path
    ):
        rahul = signed_in(user_a)
        lead = LeadFactory(owner=user_a, first_name="aaaa", last_name="", organization_name="")
        OpportunityFactory(lead=lead, value=Decimal("900.00"))
        other = LeadFactory(owner=user_a, first_name="zzzz", last_name="", organization_name="")
        OpportunityFactory(lead=other, value=Decimal("1.00"))
        first = rahul.get(f"{ME}/{list_path}", {"ordering": ordering, "page_size": 1}).json()
        assert first["next"]
        assert rahul.get(first["next"]).status_code == 200  # valid while the row is his
        lead.refresh_from_db()
        moved = signed_in(admin).post(
            f"{API}/{user_a.pk}/leads/{lead.pk}/assign",
            {"owner": str(user_b.pk), "version": lead.version},
            format="json",
        )
        assert moved.status_code == 200, moved.content
        assert is_invalid_cursor(rahul.get(first["next"]))

    def test_an_admins_organisation_cursor_can_not_measure_priya_inside_rahuls_workspace(
        self, admin, user_a, user_b
    ):
        anita = signed_in(admin)
        OpportunityFactory(lead=LeadFactory(owner=user_b), value=Decimal("888888.00"))
        OpportunityFactory(lead=LeadFactory(owner=user_b), value=Decimal("5.00"))
        OpportunityFactory(lead=LeadFactory(owner=user_a), value=Decimal("111111.00"))
        everyone = anita.get(
            f"{API}/all/opportunities", {"ordering": "-value", "page_size": 1}
        ).json()
        replayed = everyone["next"].replace(f"{API}/all/", f"{API}/{user_a.pk}/")
        assert is_invalid_cursor(anita.get(replayed))

    def test_paging_still_follows_a_boundary_row_that_changed_stage_or_status(self, user_a):
        """The re-read is scoped, not filtered: one's own row leaving the filtered set
        between pages keeps the cursor valid (no regression for the board's stage lists)."""
        rahul = signed_in(user_a)
        stage = default_stage("proposal")
        top = OpportunityFactory(
            lead=LeadFactory(owner=user_a), value=Decimal("900.00"), stage=stage
        )
        rest = OpportunityFactory(
            lead=LeadFactory(owner=user_a), value=Decimal("5.00"), stage=stage
        )
        params = {"ordering": "-value", "page_size": 1, "stage": str(stage.pk)}
        first = rahul.get(f"{ME}/opportunities", params).json()
        assert first["results"][0]["id"] == str(top.pk)
        Opportunity.objects.filter(pk=top.pk).update(stage=default_stage("negotiation"))
        second = rahul.get(first["next"])
        assert second.status_code == 200, second.content
        assert [row["id"] for row in second.json()["results"]] == [str(rest.pk)]


# --- F2 ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("archived_by_new_owner", [False, True])
def test_a_kept_deal_reveals_nothing_about_the_leads_new_workspace(
    user_a, user_b, admin, archived_by_new_owner
):
    rahul = signed_in(user_a)
    lead = LeadFactory(owner=user_a)
    won = OpportunityFactory(lead=lead, stage_key="won")
    archived_won = OpportunityFactory(lead=lead, stage_key="won", archived_at=timezone.now())
    lead.refresh_from_db()
    signed_in(admin).post(
        f"{API}/all/leads/{lead.pk}/assign",
        {"owner": str(user_b.pk), "version": lead.version},
        format="json",
    )
    if archived_by_new_owner:
        lead.refresh_from_db()
        archived = signed_in(user_b).post(
            f"{ME}/leads/{lead.pk}/archive", {"version": lead.version}, format="json"
        )
        assert archived.status_code == 200, archived.content
    reopen = rahul.post(
        f"{ME}/opportunities/{won.pk}/move",
        {"stage": str(default_stage("proposal").pk), "version": 1},
        format="json",
    )
    restore = rahul.post(
        f"{ME}/opportunities/{archived_won.pk}/restore", {"version": 1}, format="json"
    )
    # The same answers either way: reopening is refused because the lead lives elsewhere,
    # restoring his own closed deal works.
    assert reopen.status_code == 422
    assert "belongs to someone else" in reopen.json()["error"]["message"]
    assert restore.status_code == 200, restore.content


# --- F3 ---------------------------------------------------------------------------------------
def test_restoring_open_work_for_a_deactivated_owner_is_refused_like_creating_it(admin, user_a):
    lead = LeadFactory(owner=user_a)
    gone = timezone.now()
    open_deal = OpportunityFactory(lead=lead, stage_key="proposal", archived_at=gone)
    won_deal = OpportunityFactory(lead=lead, stage_key="won", archived_at=gone)
    task = TaskFactory(lead=lead, archived_at=gone)
    meeting = MeetingFactory(lead=lead, archived_at=gone)
    done = TaskFactory(lead=lead, status="completed", archived_at=gone)
    identity_services.deactivate_user(actor_id=AdminFactory().pk, user_id=user_a.pk)
    anita = signed_in(admin)
    base = f"{API}/{user_a.pk}"
    for path in (
        f"{base}/opportunities/{open_deal.pk}/restore",
        f"{base}/activities/{task.pk}/restore",
        f"{base}/activities/{meeting.pk}/restore",
    ):
        response = anita.post(path, {"version": 1}, format="json")
        assert response.status_code == 422, (path, response.content)
        assert "deactivated" in response.json()["error"]["message"]
    for record in (open_deal, task, meeting):
        record.refresh_from_db()
        assert record.archived_at is not None
    # History stays restorable: it is no new work.
    for path in (
        f"{base}/opportunities/{won_deal.pk}/restore",
        f"{base}/activities/{done.pk}/restore",
    ):
        assert anita.post(path, {"version": 1}, format="json").status_code == 200, path
