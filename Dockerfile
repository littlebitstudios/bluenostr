FROM python:3.14

WORKDIR /app

# Copy and install the package
COPY . .
RUN pip install --no-cache-dir .

ENV BLUENOSTR_USE_ENV=1

CMD ["bluenostr"]