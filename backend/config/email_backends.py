import sys
from django.core.mail.backends.smtp import EmailBackend as SmtpEmailBackend

class ConsoleAndSmtpEmailBackend(SmtpEmailBackend):
    """
    A custom email backend for local development.
    It prints the email contents to the terminal console and then 
    sends the email using the standard SMTP backend.
    """
    def send_messages(self, email_messages):
        for message in email_messages:
            print("\n" + "="*50)
            print("✉️  EMAIL INTERCEPTED (ConsoleAndSmtpEmailBackend)")
            print("="*50)
            print(f"To:      {', '.join(message.to)}")
            print(f"Subject: {message.subject}")
            print(f"From:    {message.from_email}")
            print("-" * 50)
            print(message.body)
            print("="*50 + "\n")
            sys.stdout.flush()
            
        # Proceed with normal SMTP sending
        return super().send_messages(email_messages)
