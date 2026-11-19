# Personal shared workflows

Reusable GitHub Actions workflows for repositories a personal GitHub account owns, starting with asking Claude to review a pull request.

## Language

**Review request**:
A GitHub event that asks for a Claude review of one pull request: the pull request becoming ready, a comment command, or a requested reviewer.
_Avoid_: trigger, review trigger

**Ready**:
The review request made when a non-draft pull request is opened or reopened, or a draft is marked ready for review.
_Avoid_: auto review

**Comment command**:
The review request made by the owner's comment whose first line starts, with no leading whitespace, with `> orb ponder` (the space after `>` is optional); the rest of the comment is the request's instructions.
_Avoid_: mention, slash command

**Requested reviewer**:
The review request made by requesting a review from the review agent in a pull request's Reviewers box.
_Avoid_: assigned reviewer

**Owner**:
The account that owns the repository a review request is made in, the only person allowed to make review requests; nobody else can cause a review.
_Avoid_: member, collaborator, maintainer, admin

**Review agent**:
An optional separate GitHub account that the owner requests as a reviewer to ask for a review, and under which the review is posted; none is set by default.
_Avoid_: bot, Claude user

**Routine trigger**:
The Claude code-review routine's endpoint (`trig_…`) that a review request fires to start a review session.
_Avoid_: trigger, webhook
