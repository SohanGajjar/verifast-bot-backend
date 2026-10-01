# Re-check the hold immediately before send

Auto-reply is queued after the customer's message is stored, so a takeover can commit while a reply is still waiting. The worker reads the hold again after it claims the auto-reply and before it calls Instagram; a holder marks that attempt skipped, and the claim is what stops the same message being answered after release. A send already handed to Instagram is not cancelled.
