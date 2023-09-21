const request = require('supertest');
const fs = require('fs');
const { generateFilename, saveProductJson } = require('../src/utils');
const server = require('../src/server');


const cleanTestData = () => {
    fs.rm('./test-data', { recursive: true }, (err) => {
        if(err) {
            console.log('Error deleting directory', err);
        }
    })
}


describe('scraper', () => {
    test('generateFilename() return string', ()=> {
        expect(typeof generateFilename()).toBe('string');

        expect(generateFilename()).toMatch(/\.json$/);
    })

    test('saveProductJson() saves afile', async () => {
        const products = [
            {
                title: 'Mock Product',
                price: '99.99',
                link: 'https://www.amazon.com/Mock-Product/dp/B07YXJ9XZ8'
            }
        ]

        const savedProducts = await saveProductJson(products);
        expect(typeof savedProducts).toBe('string');
        expect(savedProducts).toMatch(/^\.\/data\/.*\.json$/);
    })


    test('Post /scrape return a 200 status code', async ()=> {
        testScrapUrl =
      "https://www.amazon.com/s?k=all+headphones&crid=2TTXQBOK238J3&qid=1667301526&sprefix=all+headphones%2Caps%2C284&ref=sr_pg_1";
    })
})