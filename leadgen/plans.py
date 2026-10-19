"""The subscription plans, in one place: the landing page's prices come from here, and
billing and the plan limits are to read the same numbers.

Each plan's limits are per company (account): users who can log in, search areas
(each a circle of at most AREA_MAX_MILES radius), searches a month and phone lookups
a month.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Plan:
    key: str
    name: str
    monthly_price: int          # US dollars a month, billed monthly
    users: int
    areas: int
    searches_per_month: int
    lookups_per_month: int      # phone lookups (the paid listings data)


TRIAL_DAYS = 14
CARD_REQUIRED = True             # a card is taken at sign-up and charged when the trial ends
ANNUAL_FREE_MONTHS = 2           # billed yearly: twelve months for the price of ten
AREA_MAX_MILES = 30              # an area's radius, at most

PLANS: dict[str, Plan] = {
    "starter": Plan("starter", "Starter", monthly_price=99, users=1, areas=1,
                    searches_per_month=8, lookups_per_month=500),
    "team": Plan("team", "Team", monthly_price=249, users=5, areas=3,
                 searches_per_month=30, lookups_per_month=2500),
}


def annual_price(plan: Plan) -> int:
    """The plan's yearly price: twelve months less the free ones."""
    return plan.monthly_price * (12 - ANNUAL_FREE_MONTHS)
