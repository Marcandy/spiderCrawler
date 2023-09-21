const express = require('express');
const app = express();

const port = process.env.PORT || 3000;
const bodyParser = require('body-parser');

const scrapeProducts = require('./scraper');


app.use(bodyParser.urlencoded({ extended : true }));
app.use(bodyParser.json());

app.use('/', scrapeProducts);

server = app.listen(port, () => {
    console.log(`Server is running on ${port}`)
});


module.exports = server;
