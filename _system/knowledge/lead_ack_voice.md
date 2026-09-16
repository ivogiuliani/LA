# Lead acknowledgement — canonical text (read at runtime by lead_ack.py)

Fixed text. No LLM touches it. Placeholders in `{braces}` are filled from
`_system/config/lead_settings.yml` and from the lead record:
`{first_name}`, `{response_promise}`, `{scheduling}`, `{booking_url}`,
`{contact_email}`. The signature (`signatures.prospects`) is appended by
the policy layer, not here. No attachments. No prices. No claims of
delivered villas. Voice: warm, unhurried, first person plural.

## Subject

Your private briefing with My Villa

## Body

Dear {first_name},

Thank you for reaching out. Your request for a private briefing has arrived, and we are glad you found us.

Paolo Mezzalama, our founder, reads every enquiry himself and will reply to you personally {response_promise}.

{scheduling}

In the meantime, one question that helps us prepare: do you already have a site or lot in mind, and if so, where is it and what is its current state?

We look forward to the conversation.

Warm regards,

## Scheduling (no booking link)

To make the call easy to arrange, simply reply with two windows that suit you. Los Angeles mornings work best for a Teams call with Paolo.

## Scheduling (booking link)

To make the call easy to arrange, you can pick a slot directly here: {booking_url}
